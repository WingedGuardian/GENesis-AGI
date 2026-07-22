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

# _netres_put_file <path> <content> <mode> — write-if-different via sudo. Bumps
# _NETRES_WROTE on a real write; sets _NETRES_FAILED + WARNS on failure. Never
# fails the caller (returns 0) so `set -e` callers are safe even outside an if.
_netres_put_file() {
    local path="$1" content="$2" mode="$3"
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
    local before=$_NETRES_WROTE
    _netres_put_file "$dst" "$(cat "$NETRES_WATCHDOG_SRC")" "0755"
    _netres_put_file "$NETRES_ETC_ROOT/systemd/system/genesis-network-watchdog.service" "$(_netres_service_content "$dst")" "0644"
    _netres_put_file "$NETRES_ETC_ROOT/systemd/system/genesis-network-watchdog.timer" "$_NETRES_WATCHDOG_TIMER" "0644"
    # Reload only when a unit file actually changed this run.
    if ((_NETRES_WROTE > before)); then
        sudo systemctl daemon-reload 2>/dev/null || true
    fi
    # ALWAYS ensure the timer is active AND enabled (self-heal), decoupled from
    # whether a file changed: a re-run must re-establish a timer that was disabled/
    # stopped/masked externally since install — not skip just because the units are
    # unchanged. Both states matter: `is-active` alone misses an active-but-DISABLED
    # timer (e.g. `systemctl disable` without --now), which would silently fail to
    # persist across a reboot. Both probes are reads (no sudo) and silent, so a
    # healthy timer causes ZERO churn; only a down/unpersisted timer heals. NR1.
    if ! systemctl is-active genesis-network-watchdog.timer >/dev/null 2>&1 \
        || ! systemctl is-enabled genesis-network-watchdog.timer >/dev/null 2>&1; then
        # A masked timer can't be enabled/started until unmasked, and systemd never
        # auto-unmasks — so unmask first, then enable+start, then VERIFY. Never claim
        # a heal that didn't take: a still-down/unpersisted timer surfaces as a
        # failure (WARNING + _NETRES_FAILED), not a false "re-enabled" success.
        sudo systemctl unmask genesis-network-watchdog.timer 2>/dev/null || true
        sudo systemctl enable genesis-network-watchdog.timer 2>/dev/null || true
        sudo systemctl start genesis-network-watchdog.timer 2>/dev/null || true
        if systemctl is-active genesis-network-watchdog.timer >/dev/null 2>&1 \
            && systemctl is-enabled genesis-network-watchdog.timer >/dev/null 2>&1; then
            _NETRES_HEALED=1
        else
            echo "  WARNING: watchdog timer could not be re-enabled (may be masked or broken)."
            _NETRES_FAILED=1
        fi
    fi
}

# network_resilience_apply — the entrypoint. Always returns 0.
network_resilience_apply() {
    echo "--- Network resilience (KeepConfiguration + networkd watchdog) ---"

    if [[ ! -d "$NETRES_SYSTEMD_RUNTIME_DIR" ]]; then
        echo "  Skipped: not a systemd system (no $NETRES_SYSTEMD_RUNTIME_DIR)."
        return 0
    fi
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

    _NETRES_WROTE=0
    _NETRES_FAILED=""
    _NETRES_HEALED=""
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
