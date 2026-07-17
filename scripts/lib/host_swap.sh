# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# host_swap_apply — host-side zram swap: a compressed-RAM-first swap tier for
# the machine the Guardian runs on. Completes the swap story that
# memory_resilience.sh (#1059) deliberately left as a warning: that layer
# VERIFIES swap and warns when a bare/VM host has none; this layer CREATES a
# modest zram device so the warning has a mechanical answer. On hosts that
# already have disk swap, zram layers ABOVE it (priority 100) so memory
# pressure compresses into fast RAM before touching disk.
#
# Adaptive by design (no machine constants): device size is computed at apply
# time as min(MemTotal/2, HOSTSWAP_CAP_GIB) — zram-generator's own upstream
# default formula — so a 2 GiB VPS gets 1 GiB and a 64 GiB workstation caps at
# 4 GiB. The systemd unit is self-contained (fixed /dev/zram0, no genesis-path
# dependencies) and survives removal of the install dir.
#
# Degrades gracefully: not-systemd, container vantage (zram needs the real
# host kernel), no zramctl/modinfo, foreign zram already active (never shadow
# zram-generator or an operator's own setup), no usable sudo, an operator
# opt-out (`sudo systemctl mask zram-swap.service`), or HOSTSWAP_DISABLE=1
# each produce a one-line skip note and rc=0 — this must never abort
# install_guardian.sh or the gateway redeploy verb under `set -e`.
#
# Sourced by scripts/install_guardian.sh (fresh installs, Step 9c) and by the
# guardian-gateway.sh `redeploy` verb (existing installs retrofit on their
# next update). Idempotent: an unchanged unit is left untouched and produces
# no systemd churn.
#
# Test seams (defaults are the real system paths; pytest overrides these and
# stubs sudo/systemctl/systemd-detect-virt/modinfo/zramctl on PATH):
HOSTSWAP_ETC_ROOT="${HOSTSWAP_ETC_ROOT:-/etc}"
# The unit lives in /usr/local/lib/systemd/system (the locally-installed-unit
# layer), NOT /etc/systemd/system: `systemctl mask` works by symlinking the
# /etc path to /dev/null, so a unit file OCCUPYING that path makes mask refuse
# ("File ... already exists") — the documented opt-out would be non-functional
# (live-E2E finding 2026-07-16). /etc stays free for the operator's mask.
HOSTSWAP_UNIT_DIR="${HOSTSWAP_UNIT_DIR:-/usr/local/lib/systemd/system}"
HOSTSWAP_SYSTEMD_RUNTIME_DIR="${HOSTSWAP_SYSTEMD_RUNTIME_DIR:-/run/systemd/system}"
HOSTSWAP_MEMINFO="${HOSTSWAP_MEMINFO:-/proc/meminfo}"
HOSTSWAP_PROC_SWAPS="${HOSTSWAP_PROC_SWAPS:-/proc/swaps}"
HOSTSWAP_CAP_GIB="${HOSTSWAP_CAP_GIB:-4}"

_HOSTSWAP_UNIT_NAME="zram-swap.service"

# _hostswap_size_mib — min(MemTotal/2, cap) in MiB, from /proc/meminfo.
# Echoes the integer; echoes nothing when MemTotal is unreadable (caller skips).
_hostswap_size_mib() {
    local kb half_mib cap_mib
    kb="$(awk '/^MemTotal:/ {print $2}' "$HOSTSWAP_MEMINFO" 2>/dev/null)" || kb=""
    if [[ -z "$kb" || ! "$kb" =~ ^[0-9]+$ || "$kb" -eq 0 ]]; then
        return 0
    fi
    half_mib=$(( kb / 1024 / 2 ))
    cap_mib=$(( HOSTSWAP_CAP_GIB * 1024 ))
    if (( half_mib > cap_mib )); then
        echo "$cap_mib"
    else
        echo "$half_mib"
    fi
}

# _hostswap_unit_content <size_mib> — render the self-contained unit.
# Fixed /dev/zram0 with NO command substitution in ExecStart — no systemd
# `$`-escaping hazard. Three live-E2E findings on a real Ubuntu 6.8 host shaped
# this (2026-07-16):
#   1. `modprobe zram` ships ZERO static devices — /dev/zram0 must be created
#      by reading /sys/class/zram-control/hot_add (allocates the lowest free).
#   2. hot_add is ASYNC — udev creates the node a beat later, so `udevadm
#      settle` must follow before any userspace tool touches /dev/zram0.
#   3. `zramctl --reset` on a device makes udev REMOVE the node (not merely
#      deconfigure it), and a following configure then fails "No such device".
# So we NEVER reset: instead we always start from a fresh device — hot_remove
# a stale zram0 if one is present (the only way to resize/reconfigure safely),
# then hot_add + settle + configure. This normalizes every non-swapping case
# (cold, stale-configured, half-configured) to the one path proven to work.
# The swaps early-exit keeps restarts idempotent; the mounts guard refuses to
# touch a zram0 carrying someone's FILESYSTEM (never shows in /proc/swaps —
# a reset/remove would destroy data); the zstd fallback drops --algorithm on
# kernels without it (a fresh device has disksize 0, so --size retries cleanly).
_hostswap_unit_content() {
    local size_mib="$1"
    printf '%s\n' \
        '[Unit]' \
        'Description=Genesis zram swap (compressed-RAM-first tier)' \
        '# Installed by genesis scripts/lib/host_swap.sh — size = min(MemTotal/2, cap).' \
        '# Opt out permanently with: sudo systemctl mask zram-swap.service' \
        '' \
        '[Service]' \
        'Type=oneshot' \
        'RemainAfterExit=yes' \
        "ExecStart=/bin/sh -c 'grep -q \"^/dev/zram\" /proc/swaps && exit 0; grep -q \"^/dev/zram0 \" /proc/mounts && exit 1; modprobe zram; if [ -b /dev/zram0 ]; then echo 0 > /sys/class/zram-control/hot_remove 2>/dev/null; udevadm settle --timeout=10 2>/dev/null || true; fi; cat /sys/class/zram-control/hot_add >/dev/null; udevadm settle --timeout=10 2>/dev/null || true; zramctl /dev/zram0 --size ${size_mib}MiB --algorithm zstd || zramctl /dev/zram0 --size ${size_mib}MiB; mkswap /dev/zram0 && swapon -p 100 /dev/zram0'" \
        "ExecStop=/bin/sh -c 'swapoff /dev/zram0 2>/dev/null; [ -b /dev/zram0 ] && echo 0 > /sys/class/zram-control/hot_remove 2>/dev/null; true'" \
        '' \
        '[Install]' \
        'WantedBy=multi-user.target'
}

# _hostswap_install_unit <content> — write-if-different via sudo. Sets
# _HOSTSWAP_CHANGED=1 when it wrote (a variable, NOT stdout — mirroring
# memory_resilience.sh: capturing stdout as the change flag would let any
# future diagnostic echo masquerade as "changed" and churn systemd on
# idempotent runs). A failed write WARNS instead of falling through to
# "already in place". Never fails the caller.
_hostswap_install_unit() {
    local content="$1"
    local path="$HOSTSWAP_UNIT_DIR/$_HOSTSWAP_UNIT_NAME"
    if [[ "$(sudo cat "$path" 2>/dev/null)" == "$content" ]]; then
        return 0
    fi
    if ! sudo mkdir -p "$(dirname "$path")" 2>/dev/null \
        || ! printf '%s\n' "$content" | sudo tee "$path" >/dev/null 2>&1; then
        echo "  WARNING: could not write $path (read-only /etc?) — zram swap NOT installed."
        _HOSTSWAP_FAILED=1
        return 0
    fi
    _HOSTSWAP_CHANGED=1
    return 0
}

# host_swap_apply — the entrypoint. Always returns 0.
host_swap_apply() {
    echo "--- Host zram swap (compressed-RAM-first tier) ---"

    if [[ "${HOSTSWAP_DISABLE:-0}" == "1" ]]; then
        echo "  Skipped: HOSTSWAP_DISABLE=1."
        return 0
    fi
    if [[ ! -d "$HOSTSWAP_SYSTEMD_RUNTIME_DIR" ]]; then
        echo "  Skipped: not a systemd system (no $HOSTSWAP_SYSTEMD_RUNTIME_DIR)."
        return 0
    fi
    if systemd-detect-virt --container >/dev/null 2>&1; then
        echo "  Skipped: container vantage — zram needs the real host kernel (configure swap on the host)."
        return 0
    fi
    if ! command -v zramctl >/dev/null 2>&1; then
        echo "  Skipped: zramctl not available (util-linux)."
        return 0
    fi
    if ! modinfo zram >/dev/null 2>&1; then
        echo "  Skipped: zram kernel module not available on this kernel."
        return 0
    fi
    local unit_path="$HOSTSWAP_UNIT_DIR/$_HOSTSWAP_UNIT_NAME"
    local legacy_unit="$HOSTSWAP_ETC_ROOT/systemd/system/$_HOSTSWAP_UNIT_NAME"
    # Never shadow or DESTROY an external zram consumer (zram-generator,
    # Fedora defaults, a zram-backed mount, an operator's own unit): any
    # configured zram device — swap OR non-swap (a zram mount never appears
    # in /proc/swaps, but our unit's reset would wipe it) — while OUR unit
    # was never installed → leave it alone.
    if [[ ! -f "$unit_path" && ! -f "$legacy_unit" ]]; then
        if grep -q '^/dev/zram' "$HOSTSWAP_PROC_SWAPS" 2>/dev/null \
            || [[ -n "$(zramctl --noheadings 2>/dev/null || true)" ]]; then
            echo "  Skipped: external zram device already configured — not touching it."
            return 0
        fi
    fi
    if ! sudo -n true 2>/dev/null; then
        echo "  Skipped: sudo unavailable non-interactively. To apply manually:"
        echo "           sudo bash -c 'source scripts/lib/host_swap.sh && host_swap_apply'"
        return 0
    fi
    # Operator opt-out: a masked unit means "leave my swap alone", durably.
    if [[ "$(systemctl is-enabled "$_HOSTSWAP_UNIT_NAME" 2>/dev/null)" == "masked" ]]; then
        echo "  Skipped: $_HOSTSWAP_UNIT_NAME is masked (operator opt-out). Unmask to re-enable."
        return 0
    fi

    local size_mib
    size_mib="$(_hostswap_size_mib)"
    if [[ -z "$size_mib" ]]; then
        echo "  WARNING: cannot read MemTotal from $HOSTSWAP_MEMINFO — zram swap NOT installed."
        return 0
    fi

    _HOSTSWAP_CHANGED=""
    _HOSTSWAP_FAILED=""
    _hostswap_install_unit "$(_hostswap_unit_content "$size_mib")"

    # Migrate a legacy /etc-installed unit (pre-2026-07-16 installs): it would
    # SHADOW the /usr/local unit by precedence and keep mask broken. Removed
    # only AFTER the replacement write succeeded (Codex P2: deleting the
    # working unit first + a failed write would leave the host with NO unit);
    # on a failed write the legacy copy stays and keeps the host protected.
    # A first migration is always a CHANGED write (new path was empty), so
    # daemon-reload + restart below relocate atomically.
    if [[ -f "$legacy_unit" ]]; then
        if [[ -z "$_HOSTSWAP_FAILED" ]]; then
            sudo rm -f "$legacy_unit" 2>/dev/null || true
            echo "  migrated: removed legacy $legacy_unit (unit now lives in $HOSTSWAP_UNIT_DIR)"
        else
            echo "  keeping legacy $legacy_unit (replacement write failed — still protected)."
        fi
    fi

    if [[ -n "$_HOSTSWAP_FAILED" ]]; then
        return 0
    fi
    # `restart` (not `enable --now`) is deliberate: the unit is a
    # RemainAfterExit oneshot, so once it has succeeded it stays "active" and
    # `enable --now`/`start` become no-ops — they would NOT apply changed unit
    # content, nor recover a device that was swapped off out from under an
    # "active" unit. `restart` always re-runs ExecStop (frees the old device)
    # then ExecStart (fresh device), which is what actually applies/heals.
    # `enable` (without --now) just sets boot persistence.
    if [[ -n "$_HOSTSWAP_CHANGED" ]]; then
        # Best-effort: a hiccup here must never abort the caller — the unit is
        # on disk and the next boot applies it regardless.
        sudo systemctl daemon-reload 2>/dev/null || true
        sudo systemctl enable "$_HOSTSWAP_UNIT_NAME" 2>/dev/null || true
        sudo systemctl restart "$_HOSTSWAP_UNIT_NAME" 2>/dev/null || true
        echo "  + zram swap unit installed (size ${size_mib}MiB, priority 100)."
    elif ! grep -q '^/dev/zram' "$HOSTSWAP_PROC_SWAPS" 2>/dev/null; then
        # Unchanged unit but no active zram swap: a previous start failed, or
        # swap was removed from under the "active" unit. The durable opt-out is
        # MASK (guarded above) — a merely-disabled/failed unit is re-started,
        # so a transient failure heals on the next apply instead of sticking.
        # (No churn on healthy hosts: an active device skips this branch.)
        sudo systemctl enable "$_HOSTSWAP_UNIT_NAME" 2>/dev/null || true
        sudo systemctl restart "$_HOSTSWAP_UNIT_NAME" 2>/dev/null || true
        echo "  zram swap unit already in place (size ${size_mib}MiB) — restarted (was inactive)."
    else
        # Healthy device but boot persistence may have been lost: `systemctl
        # unmask` sweeps the multi-user.target.wants symlink along with the
        # mask (live-E2E finding 2026-07-16), leaving the unit active-but-
        # disabled — zram would silently not start on the next boot. Re-enable
        # when needed; a no-op (read-only is-enabled) on healthy hosts.
        if [[ "$(systemctl is-enabled "$_HOSTSWAP_UNIT_NAME" 2>/dev/null)" != "enabled" ]]; then
            sudo systemctl enable "$_HOSTSWAP_UNIT_NAME" 2>/dev/null || true
            echo "  zram swap unit already in place (size ${size_mib}MiB) — re-enabled for boot."
        else
            echo "  zram swap unit already in place (size ${size_mib}MiB)."
        fi
    fi

    # Verify the OUTCOME, not just the unit state: is a zram device actually
    # swapping? (oneshot ExecStart runs synchronously under restart.)
    if grep -q '^/dev/zram' "$HOSTSWAP_PROC_SWAPS" 2>/dev/null; then
        echo "  + zram swap active: $(grep '^/dev/zram' "$HOSTSWAP_PROC_SWAPS" 2>/dev/null | awk '{print $1, "prio", $5}' | head -1)"
    else
        echo "  WARNING: zram swap not (yet) active — check: systemctl status $_HOSTSWAP_UNIT_NAME"
    fi
    return 0
}

# host_swap_remove — the clean opt-out / rollback path (operator-invoked):
#   sudo is required; removes the device, the unit, and reloads systemd.
# Pair with `sudo systemctl mask zram-swap.service` to prevent re-apply on
# the next update. Always returns 0.
host_swap_remove() {
    echo "--- Removing host zram swap ---"
    if ! sudo -n true 2>/dev/null; then
        echo "  Skipped: sudo unavailable non-interactively."
        return 0
    fi
    sudo systemctl disable --now "$_HOSTSWAP_UNIT_NAME" 2>/dev/null || true
    sudo swapoff /dev/zram0 2>/dev/null || true
    # hot_remove fully frees the device (a bare --reset leaves a stale node);
    # ExecStop already ran on disable --now, so this is the belt to its braces.
    sudo sh -c 'test -b /dev/zram0 && echo 0 > /sys/class/zram-control/hot_remove' 2>/dev/null || true
    sudo rm -f "$HOSTSWAP_UNIT_DIR/$_HOSTSWAP_UNIT_NAME" 2>/dev/null || true
    # The /etc path is removed only when it is a REGULAR file (a legacy
    # pre-relocation unit). After `systemctl mask` it is the operator's
    # /dev/null SYMLINK — deleting it would silently destroy the opt-out and
    # the next apply would reinstall (Codex P2). mask survives remove.
    local _legacy="$HOSTSWAP_ETC_ROOT/systemd/system/$_HOSTSWAP_UNIT_NAME"
    if [[ -f "$_legacy" && ! -L "$_legacy" ]]; then
        sudo rm -f "$_legacy" 2>/dev/null || true
    fi
    sudo systemctl daemon-reload 2>/dev/null || true
    echo "  + zram swap removed (mask the unit to prevent re-apply on update)."
    return 0
}
