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
# Fixed /dev/zram0 (modprobe creates it by default) means NO command
# substitution inside ExecStart — no systemd `$`-escaping hazard. The
# swaps early-exit keeps restarts idempotent (a second swapon would fail);
# the hot_add read creates the device when the module ships ZERO static
# devices (Ubuntu 6.8 does — live-E2E finding 2026-07-16; reading
# /sys/class/zram-control/hot_add allocates the lowest free device);
# the mounts guard refuses to touch a zram0 that carries someone's
# FILESYSTEM (a zram-backed mount never shows in /proc/swaps — resetting it
# would destroy data); the --reset before configure clears any stale
# half-configured state; the fallback chain drops --algorithm zstd on
# kernels without it.
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
        "ExecStart=/bin/sh -c 'grep -q \"^/dev/zram\" /proc/swaps && exit 0; grep -q \"^/dev/zram0 \" /proc/mounts && exit 1; modprobe zram; test -b /dev/zram0 || cat /sys/class/zram-control/hot_add >/dev/null; zramctl --reset /dev/zram0 2>/dev/null; zramctl /dev/zram0 --size ${size_mib}MiB --algorithm zstd 2>/dev/null || { zramctl --reset /dev/zram0 2>/dev/null; zramctl /dev/zram0 --size ${size_mib}MiB; }; mkswap /dev/zram0 && swapon -p 100 /dev/zram0'" \
        "ExecStop=/bin/sh -c 'swapoff /dev/zram0 2>/dev/null; zramctl --reset /dev/zram0 2>/dev/null; true'" \
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
    local path="$HOSTSWAP_ETC_ROOT/systemd/system/$_HOSTSWAP_UNIT_NAME"
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
    local unit_path="$HOSTSWAP_ETC_ROOT/systemd/system/$_HOSTSWAP_UNIT_NAME"
    # Never shadow or DESTROY an external zram consumer (zram-generator,
    # Fedora defaults, a zram-backed mount, an operator's own unit): any
    # configured zram device — swap OR non-swap (a zram mount never appears
    # in /proc/swaps, but our unit's reset would wipe it) — while OUR unit
    # was never installed → leave it alone.
    if [[ ! -f "$unit_path" ]]; then
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

    if [[ -n "$_HOSTSWAP_FAILED" ]]; then
        return 0
    fi
    if [[ -n "$_HOSTSWAP_CHANGED" ]]; then
        # Best-effort: a hiccup here must never abort the caller — the unit is
        # on disk and the next boot applies it regardless.
        sudo systemctl daemon-reload 2>/dev/null || true
        sudo systemctl enable --now "$_HOSTSWAP_UNIT_NAME" 2>/dev/null || true
        echo "  + zram swap unit installed (size ${size_mib}MiB, priority 100)."
    elif ! grep -q '^/dev/zram' "$HOSTSWAP_PROC_SWAPS" 2>/dev/null; then
        # Unchanged unit but no active zram swap: a previous enable failed, or
        # an operator `disable`d without masking. The durable opt-out is MASK
        # (guarded above) — a merely-disabled unit gets re-enabled, and a
        # transient enable failure retries on the next apply instead of
        # sticking forever. (No churn on healthy hosts: an active device
        # skips this branch.)
        sudo systemctl enable --now "$_HOSTSWAP_UNIT_NAME" 2>/dev/null || true
        echo "  zram swap unit already in place (size ${size_mib}MiB) — re-enabled (was inactive)."
    else
        echo "  zram swap unit already in place (size ${size_mib}MiB)."
    fi

    # Verify the OUTCOME, not just the unit state: is a zram device actually
    # swapping? (oneshot ExecStart runs synchronously under enable --now.)
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
    sudo zramctl --reset /dev/zram0 2>/dev/null || true
    sudo rm -f "$HOSTSWAP_ETC_ROOT/systemd/system/$_HOSTSWAP_UNIT_NAME" 2>/dev/null || true
    sudo systemctl daemon-reload 2>/dev/null || true
    echo "  + zram swap removed (mask the unit to prevent re-apply on update)."
    return 0
}
