# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# network_resilience_apply — codify the 2026-07 eth0 networkd-wedge fix. Under
# memory pressure the container's systemd-networkd hit rtnetlink timeouts
# (`eth0: Could not set route: Connection timed out` → `eth0: Failed`) three
# times in three days; the DHCP lease was then dropped and the box fell off the
# network until a MANUAL `systemctl restart systemd-networkd` hours later. Two
# layers, both codified here so fresh installs are born protected:
#
#   A. KeepConfiguration=true on the default-route link — a networkd failure
#      RETAINS the address (renewals pause) instead of dropping it. Adaptive:
#      the link and its .network unit are discovered live, never hardcoded.
#   B. A root networkd watchdog (genesis-network-watchdog.timer) that detects a
#      failed/inactive/route-less networkd and restarts it — automating the
#      manual recovery. The restart is address-preserving under (A).
#   C. A root Tailscale watchdog (genesis-tailscale-watchdog.timer) that heals a
#      stuck Tailscale tunnel (scripts/systemd/genesis-tailscale-watchdog.py).
#      It does not depend on networkd, so it installs on any systemd host with a
#      tailscaled unit, before the networkd gates below.
#
# Degrades gracefully: no systemd, no networkd, no networkctl, or no usable
# sudo each produce a one-line skip note and rc=0 — must never abort
# bootstrap.sh/update.sh under `set -e`. Idempotent: unchanged files produce no
# reload/restart churn.
#
# Test seams (defaults are the real paths; pytest overrides these and stubs
# sudo/systemctl/networkctl/ip on PATH):
NETRES_ETC_ROOT="${NETRES_ETC_ROOT:-/etc}"
NETRES_SYSTEMD_RUNTIME_DIR="${NETRES_SYSTEMD_RUNTIME_DIR:-/run/systemd/system}"
NETRES_LIBEXEC_DIR="${NETRES_LIBEXEC_DIR:-/usr/local/lib/genesis}"
# The watchdog script shipped alongside this lib (scripts/systemd/…). Resolved
# at source time from this file's own location; overridable for tests. The
# trailing `; true` keeps a failed resolution from aborting the sourcing caller
# under `set -e` — a bad path is caught later by the readable-source guard.
NETRES_WATCHDOG_SRC="${NETRES_WATCHDOG_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../systemd" 2>/dev/null && pwd; true)/genesis-network-watchdog.sh}"
NETRES_TS_WATCHDOG_SRC="${NETRES_TS_WATCHDOG_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../systemd" 2>/dev/null && pwd; true)/genesis-tailscale-watchdog.py}"
# The Tailscale watchdog runs under the HOST's Python (standard library only),
# never the Genesis venv, which root does not own and an update can rebuild.
NETRES_PYTHON="${NETRES_PYTHON:-/usr/bin/python3}"

# KeepConfiguration=true (superset of =dhcp): retains BOTH DHCP-provided and
# static/foreign config across a networkd failure or daemon stop. `true` is
# exactly what netplan `critical: true` renders to, so one drop-in delivers the
# full hand-applied protection. Cost (documented in the runbook): re-addressing
# an interface then needs a full networkd restart, since reconfigure requests
# won't tear down kept config.
_NETRES_KEEP_CONF=$'[Network]\nKeepConfiguration=true'

_NETRES_WATCHDOG_TIMER=$'[Unit]\nDescription=Genesis network watchdog — heal a wedged systemd-networkd\n\n[Timer]\nOnBootSec=3min\nOnUnitActiveSec=2min\nAccuracySec=20s\n\n[Install]\nWantedBy=timers.target'

# Service content is built from the install dir so the ExecStart path tracks
# NETRES_LIBEXEC_DIR (real install and test overrides stay consistent).
_netres_service_content() {
    printf '%s\n' \
        '[Unit]' \
        'Description=Genesis network watchdog (heal wedged systemd-networkd)' \
        'After=systemd-networkd.service' \
        '' \
        '[Service]' \
        'Type=oneshot' \
        "ExecStart=$1"
}

_NETRES_TS_TIMER=$'[Unit]\nDescription=Genesis Tailscale watchdog — heal a stuck Tailscale tunnel\n\n[Timer]\nOnBootSec=3min\nOnUnitActiveSec=2min\nAccuracySec=20s\n\n[Install]\nWantedBy=timers.target'

# TimeoutStartSec bounds a HUNG run; the oneshot otherwise has no start timeout
# and a hang would stop its timer. The worst legitimate run under every
# setting's maximum (genesis-tailscale-watchdog.py SETTINGS), with every phase
# overrunning its own deadline by the one call in flight, is about 55 min;
# tests/test_scripts/test_network_resilience.py derives it from SETTINGS and
# fails if this limit does not clear it by 5 min. 65min clears it; the
# defaults take ~6 min.
_netres_ts_service_content() {
    printf '%s\n' \
        '[Unit]' \
        'Description=Genesis Tailscale watchdog (heal a stuck Tailscale tunnel)' \
        'After=tailscaled.service' \
        '' \
        '[Service]' \
        'Type=oneshot' \
        'TimeoutStartSec=65min' \
        '# Root resolves tailscale and systemctl by name: pin PATH to root-owned' \
        "# directories (systemd's own default for system services, made explicit)." \
        'Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
        "ExecStart=$NETRES_PYTHON $1"
}

# _netres_put_file <path> <content> <mode> — write-if-different via sudo. Bumps
# _NETRES_WROTE on a real write; sets _NETRES_FAILED + WARNS on failure. Never
# fails the caller (returns 0) so `set -e` callers are safe even outside an if.
_netres_put_file() {
    local path="$1" content="$2" mode="$3"
    # Never write THROUGH a symlink. A masked unit is a symlink to /dev/null:
    # tee would discard the file and chmod would change /dev/null itself, which
    # breaks every non-root process that writes to it.
    if sudo test -L "$path" 2>/dev/null; then
        echo "  WARNING: $path is a symlink (a masked unit?) — not writing through it; network resilience NOT fully applied."
        _NETRES_FAILED=1
        return 0
    fi
    if [[ "$(sudo cat "$path" 2>/dev/null)" == "$content" ]]; then
        return 0
    fi
    if ! sudo mkdir -p "$(dirname "$path")" 2>/dev/null \
        || ! printf '%s\n' "$content" | sudo tee "$path" >/dev/null 2>&1; then
        echo "  WARNING: could not write $path (read-only /etc?) — network resilience NOT fully applied."
        _NETRES_FAILED=1
        return 0
    fi
    sudo chmod "$mode" "$path" 2>/dev/null || true
    _NETRES_WROTE=$((_NETRES_WROTE + 1))
    return 0
}

# Part A — KeepConfiguration on the live default-route interface.
_netres_apply_keepconfig() {
    local iface
    iface="$(ip -j route show default 2>/dev/null | python3 -c '
import json, sys
try:
    routes = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for route in routes:
    if route.get("dev"):
        print(route["dev"])
        break
' 2>/dev/null)"
    if [[ -z "$iface" ]]; then
        echo "  KeepConfiguration: no IPv4 default route — skipping (nothing to protect)."
        return 0
    fi

    # Resolve the .network unit governing this link (its basename names the
    # drop-in dir). networkctl prints e.g. "Network File: /run/systemd/network/
    # 10-netplan-eth0.network"; a link with no unit prints "n/a".
    local netfile
    netfile="$(networkctl status "$iface" 2>/dev/null | sed -n 's/.*Network File:[[:space:]]*//p' | head -1)"
    if [[ -z "$netfile" || "$netfile" == "n/a" ]]; then
        echo "  KeepConfiguration: no .network unit resolved for $iface — skipping."
        return 0
    fi

    local base dropin
    base="$(basename "$netfile")"
    dropin="$NETRES_ETC_ROOT/systemd/network/$base.d/genesis-keep-config.conf"
    local before=$_NETRES_WROTE
    _netres_put_file "$dropin" "$_NETRES_KEEP_CONF" "0644"
    if ((_NETRES_WROTE > before)); then
        # Apply without a full restart: reload re-reads unit config; the kept
        # (critical) address is not torn down.
        sudo networkctl reload 2>/dev/null || true
        echo "  KeepConfiguration=true set for $iface ($base)."
    fi
}

# Part B — install the watchdog script + oneshot service + timer.
_netres_install_watchdog() {
    if [[ ! -r "$NETRES_WATCHDOG_SRC" ]]; then
        echo "  WARNING: watchdog source missing ($NETRES_WATCHDOG_SRC) — watchdog NOT installed."
        _NETRES_FAILED=1
        return 0
    fi
    local dst="$NETRES_LIBEXEC_DIR/network-watchdog.sh"
    # If the timer unit is MASKED, unmask FIRST (only when actually masked, so
    # healthy runs stay churn-free). Two variants, both handled:
    #  • "masked" (persistent): the /etc unit path is a symlink to /dev/null, and
    #    the writes below use `tee` (follows symlinks) → they'd hit /dev/null and
    #    never create the real unit; unmasking first lets the rewrite recreate it.
    #  • "masked-runtime" (`mask --runtime`): a /run symlink shadows the unit so
    #    enable/start fail until it's removed.
    # `is-enabled` reports "masked" or "masked-runtime"; match both. NR1.
    local _wd_state
    _wd_state="$(systemctl is-enabled genesis-network-watchdog.timer 2>/dev/null || true)"
    if [[ "$_wd_state" == masked* ]]; then
        sudo systemctl unmask genesis-network-watchdog.timer 2>/dev/null || true
    fi
    local before=$_NETRES_WROTE
    _netres_put_file "$dst" "$(cat "$NETRES_WATCHDOG_SRC")" "0755"
    _netres_put_file "$NETRES_ETC_ROOT/systemd/system/genesis-network-watchdog.service" "$(_netres_service_content "$dst")" "0644"
    _netres_put_file "$NETRES_ETC_ROOT/systemd/system/genesis-network-watchdog.timer" "$_NETRES_WATCHDOG_TIMER" "0644"
    # Reload only when a unit file actually changed this run.
    if ((_NETRES_WROTE > before)); then
        sudo systemctl daemon-reload 2>/dev/null || true
    fi
    _netres_ensure_timer_enabled genesis-network-watchdog.timer "watchdog timer"
}

# _netres_ensure_timer_enabled <timer> <label> — ALWAYS ensure the timer is
# active AND enabled (self-heal), decoupled from whether a file changed: a
# re-run must re-establish a timer that was disabled/stopped externally since
# install, not skip just because the units are unchanged. Both states matter:
# `is-active` alone misses an active-but-DISABLED timer (e.g. `systemctl
# disable` without --now), which would silently fail to persist across a
# reboot. Both probes are reads (no sudo) and silent, so a healthy timer causes
# ZERO churn; only a down/unpersisted timer heals. NR1.
# Heal unless the timer is active AND *persistently* enabled. `is-enabled`
# exits 0 for BOTH "enabled" and "enabled-runtime" — but the latter is
# transient (stored under /run, gone on reboot), exactly what persistence must
# prevent — so key on stdout being EXACTLY "enabled" (matching the posture
# collector's *_watchdog_enabled signals), not the exit status.
_netres_ensure_timer_enabled() {
    local timer="$1" label="$2" _wd_enabled
    _wd_enabled="$(systemctl is-enabled "$timer" 2>/dev/null || true)"
    if ! systemctl is-active "$timer" >/dev/null 2>&1 \
        || [ "$_wd_enabled" != "enabled" ]; then
        # enable+start, then VERIFY — never claim a heal that didn't take: a
        # still-down or not-persistently-enabled timer surfaces as WARNING +
        # _NETRES_FAILED.
        sudo systemctl enable "$timer" 2>/dev/null || true
        sudo systemctl start "$timer" 2>/dev/null || true
        if systemctl is-active "$timer" >/dev/null 2>&1 \
            && [ "$(systemctl is-enabled "$timer" 2>/dev/null || true)" = "enabled" ]; then
            _NETRES_HEALED=1
        else
            echo "  WARNING: $label could not be re-enabled (may be masked or broken)."
            _NETRES_FAILED=1
        fi
    fi
}

# Part C — install the Tailscale watchdog (helper + oneshot service + timer).
# Its own gates, independent of networkd: a systemd host with a tailscaled unit,
# the host's Python 3.8+, and non-interactive sudo. The durable off switch is
# NETWD_TS_MODE=off (or observe) in a drop-in on the service: a unit installed
# in /etc/systemd/system cannot be masked. A mask that does exist (the timer or
# service masked before it was ever installed) is still respected: nothing is
# written or enabled. (The networkd timer above unmasks instead; that is its
# older, deliberate contract, NR1.)
_netres_install_tailscale_watchdog() {
    if ! systemctl cat tailscaled.service >/dev/null 2>&1; then
        echo "  Tailscale watchdog: no tailscaled unit on this host — skipping."
        return 0
    fi
    local _unit
    for _unit in genesis-tailscale-watchdog.timer genesis-tailscale-watchdog.service; do
        if [[ "$(systemctl is-enabled "$_unit" 2>/dev/null || true)" == masked* ]]; then
            echo "  Tailscale watchdog: $_unit is masked (operator off switch) — leaving it alone."
            return 0
        fi
    done
    if ! "$NETRES_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then
        echo "  WARNING: Tailscale watchdog needs $NETRES_PYTHON (3.8+) — NOT installed."
        _NETRES_FAILED=1
        return 0
    fi
    if ! sudo -n true 2>/dev/null; then
        echo "  Tailscale watchdog: sudo unavailable non-interactively — skipping."
        return 0
    fi
    if [[ ! -r "$NETRES_TS_WATCHDOG_SRC" ]]; then
        echo "  WARNING: Tailscale watchdog source missing ($NETRES_TS_WATCHDOG_SRC) — NOT installed."
        _NETRES_FAILED=1
        return 0
    fi
    local dst="$NETRES_LIBEXEC_DIR/tailscale-watchdog.py" before=$_NETRES_WROTE
    _netres_put_file "$dst" "$(cat "$NETRES_TS_WATCHDOG_SRC")" "0755"
    _netres_put_file "$NETRES_ETC_ROOT/systemd/system/genesis-tailscale-watchdog.service" "$(_netres_ts_service_content "$dst")" "0644"
    _netres_put_file "$NETRES_ETC_ROOT/systemd/system/genesis-tailscale-watchdog.timer" "$_NETRES_TS_TIMER" "0644"
    if ((_NETRES_WROTE > before)); then
        sudo systemctl daemon-reload 2>/dev/null || true
        echo "  Tailscale watchdog installed ($dst)."
    fi
    _netres_ensure_timer_enabled genesis-tailscale-watchdog.timer "Tailscale watchdog timer"
}

# network_resilience_apply — the entrypoint. Always returns 0.
network_resilience_apply() {
    echo "--- Network resilience (KeepConfiguration + networkd and Tailscale watchdogs) ---"

    if [[ ! -d "$NETRES_SYSTEMD_RUNTIME_DIR" ]]; then
        echo "  Skipped: not a systemd system (no $NETRES_SYSTEMD_RUNTIME_DIR)."
        return 0
    fi

    _NETRES_WROTE=0
    _NETRES_FAILED=""
    _NETRES_HEALED=""
    # Part C first: it does not need networkd, so the gates below must not
    # skip it. It prints its own lines.
    _netres_install_tailscale_watchdog
    if ! command -v networkctl >/dev/null 2>&1; then
        echo "  Skipped: networkctl not present — not a systemd-networkd system."
        return 0
    fi
    # systemd-networkd must be THIS box's manager — but "active" alone is too
    # strict: a networkd that has crashed/stopped (exactly the state the
    # watchdog exists to heal) is inactive yet still the manager. Treat it as
    # ours if active OR enabled; skip only genuine NetworkManager/other stacks
    # (networkd disabled/masked/absent). Part A self-degrades when networkd is
    # down (networkctl discovery fails → skips); Part B still installs.
    _netres_enabled_state="$(systemctl is-enabled systemd-networkd 2>/dev/null || true)"
    if ! systemctl is-active systemd-networkd >/dev/null 2>&1 \
        && [[ "$_netres_enabled_state" != "enabled" && "$_netres_enabled_state" != "enabled-runtime" ]]; then
        echo "  Skipped: systemd-networkd not active or enabled — not this host's network manager."
        return 0
    fi
    if ! sudo -n true 2>/dev/null; then
        echo "  Skipped: sudo unavailable non-interactively. To apply manually:"
        echo "           sudo bash -c 'source scripts/lib/network_resilience.sh && network_resilience_apply'"
        return 0
    fi

    _netres_apply_keepconfig
    _netres_install_watchdog

    if [[ -n "$_NETRES_FAILED" ]]; then
        echo "  Network resilience NOT fully applied — see warnings above."
    elif ((_NETRES_WROTE > 0)); then
        echo "  Network resilience applied (KeepConfiguration + watchdog timer active)."
    elif [[ -n "$_NETRES_HEALED" ]]; then
        echo "  Network resilience: re-enabled a stopped/disabled watchdog timer."
    else
        echo "  Network resilience already in place."
    fi
    return 0
}
