# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# memory_resilience_apply — adaptive OOM-resilience setup for the machine
# Genesis runs on. Codifies the 2026-07 blast-radius Layer-3 fix: a memory
# spike must degrade into swap pressure + a userspace OOM kill of the greedy
# process tree, never into load-100 D-state thrash that wedges the box (the
# failure mode behind four separate incidents: no swap + no oomd meant the
# kernel had nowhere to reclaim to and nothing killed the offender).
#
# Adaptive by design (no absolute byte limits anywhere):
#   - systemd-oomd acts on PSI memory-pressure PERCENTAGES, so the same
#     config right-sizes from an 8 GiB VPS to a 64 GiB workstation.
#   - Swap itself is only VERIFIED here, never created: inside a container
#     the knob lives on the host (incus `limits.memory.swap`), and on bare
#     metal creating swap is an operator decision (disk layout varies).
#     We warn with the exact remediation for the detected vantage.
#
# Degrades gracefully: no systemd, no systemd-oomd, no PSI, or no usable
# sudo each produce a one-line skip note and rc=0 — this must never abort
# bootstrap.sh/update.sh under `set -e`.
#
# Sourced by scripts/bootstrap.sh (fresh installs) and scripts/update.sh
# (existing installs retrofit on their next update). Idempotent: unchanged
# drop-ins are left untouched and produce no systemd churn.
#
# Test seams (defaults are the real system paths; pytest overrides these and
# stubs sudo/systemctl/swapon/systemd-detect-virt on PATH):
MEMRES_ETC_ROOT="${MEMRES_ETC_ROOT:-/etc}"
MEMRES_SYSTEMD_RUNTIME_DIR="${MEMRES_SYSTEMD_RUNTIME_DIR:-/run/systemd/system}"
MEMRES_PSI_FILE="${MEMRES_PSI_FILE:-/proc/pressure/memory}"
MEMRES_CGROUP_SWAP_MAX="${MEMRES_CGROUP_SWAP_MAX:-/sys/fs/cgroup/memory.swap.max}"

# The pressure policy, mirrored from the config proven live on the reference
# install (2026-07-13 incident fix). user.slice is the backstop at 60%; the
# per-user manager (user@N.service) is the operative monitor — Ubuntu ships a
# kill@50% default for it, and our drop-in guarantees `kill` on distros that
# ship `auto` or nothing (limit then falls back to DefaultMemoryPressureLimit).
# SwapUsedLimit only acts where oomd can see swap (bare/VM installs; inside a
# container the host owns swap and the pressure path is the active mechanism).
_MEMRES_USER_SLICE_CONF=$'[Slice]\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=60%'
_MEMRES_USER_SERVICE_CONF=$'[Service]\nManagedOOMMemoryPressure=kill'
_MEMRES_OOMD_CONF=$'[OOM]\nSwapUsedLimit=90%\nDefaultMemoryPressureLimit=60%\nDefaultMemoryPressureDurationSec=20s'

# _memres_install_dropin <abs-path> <content> — write-if-different via sudo.
# Prints "changed" when it wrote. Never fails the caller.
_memres_install_dropin() {
    local path="$1" content="$2"
    if [[ "$(sudo cat "$path" 2>/dev/null)" == "$content" ]]; then
        return 0
    fi
    sudo mkdir -p "$(dirname "$path")" 2>/dev/null || return 0
    if printf '%s\n' "$content" | sudo tee "$path" >/dev/null 2>&1; then
        echo "changed"
    fi
    return 0
}

# _memres_swap_check — warn (never fail) when the swap invariant is violated.
# The invariant: the memory cgroup must be ABLE to swap (swap.max != 0) and,
# outside containers, a swap device must exist. Violation = the wedge defect.
_memres_swap_check() {
    local swap_max=""
    if [[ -r "$MEMRES_CGROUP_SWAP_MAX" ]]; then
        swap_max="$(cat "$MEMRES_CGROUP_SWAP_MAX" 2>/dev/null)"
    fi

    if [[ "$swap_max" == "0" ]]; then
        if systemd-detect-virt --container >/dev/null 2>&1; then
            echo "  WARNING: cgroup memory.swap.max is 0 — memory spikes will thrash instead of swapping."
            echo "           Fix on the HOST (not from inside this container):"
            echo "             incus config set <container-name> limits.memory.swap true"
            echo "           and ensure the host itself has swap (swapon --show)."
        else
            echo "  WARNING: cgroup memory.swap.max is 0 — memory spikes will thrash instead of swapping."
            echo "           Remove the swap.max=0 override (systemctl show -p MemorySwapMax) or re-enable swap."
        fi
        return 0
    fi

    # Bare/VM vantage: no swap device at all is the same defect one layer down.
    if ! systemd-detect-virt --container >/dev/null 2>&1; then
        if [[ -z "$(swapon --noheadings --show 2>/dev/null)" ]]; then
            echo "  WARNING: no swap device configured — memory pressure has nowhere to reclaim to."
            echo "           Add swap (swapfile or LV) sized to taste; even a few GiB turns the OOM cliff into a ramp."
        fi
    fi
    return 0
}

# memory_resilience_apply — the entrypoint. Always returns 0.
memory_resilience_apply() {
    echo "--- Memory resilience (systemd-oomd + swap invariant) ---"

    if [[ ! -d "$MEMRES_SYSTEMD_RUNTIME_DIR" ]]; then
        echo "  Skipped: not a systemd system (no $MEMRES_SYSTEMD_RUNTIME_DIR)."
        return 0
    fi
    if ! systemctl list-unit-files systemd-oomd.service --no-legend 2>/dev/null | grep -q systemd-oomd; then
        echo "  Skipped: systemd-oomd not available on this system (install the systemd-oomd package to enable pressure-kill protection)."
        return 0
    fi
    if [[ ! -f "$MEMRES_PSI_FILE" ]]; then
        echo "  Skipped: kernel PSI not available ($MEMRES_PSI_FILE missing) — systemd-oomd needs pressure accounting."
        return 0
    fi
    if ! sudo -n true 2>/dev/null; then
        echo "  Skipped: sudo unavailable non-interactively. To apply manually:"
        echo "           sudo bash -c 'source scripts/lib/memory_resilience.sh && memory_resilience_apply'"
        return 0
    fi

    local changed=""
    [[ -n "$(_memres_install_dropin "$MEMRES_ETC_ROOT/systemd/system/user.slice.d/genesis-oomd.conf" "$_MEMRES_USER_SLICE_CONF")" ]] && changed=1
    [[ -n "$(_memres_install_dropin "$MEMRES_ETC_ROOT/systemd/system/user@.service.d/genesis-oomd.conf" "$_MEMRES_USER_SERVICE_CONF")" ]] && changed=1
    [[ -n "$(_memres_install_dropin "$MEMRES_ETC_ROOT/systemd/oomd.conf.d/genesis.conf" "$_MEMRES_OOMD_CONF")" ]] && changed=1

    if [[ -n "$changed" ]]; then
        # daemon-reload propagates the ManagedOOM* unit properties to oomd;
        # the oomd restart picks up oomd.conf.d. Best-effort: a hiccup here
        # must never abort bootstrap/update — the drop-ins are on disk and
        # the next boot applies them regardless.
        sudo systemctl daemon-reload 2>/dev/null || true
        sudo systemctl enable --now systemd-oomd 2>/dev/null || true
        sudo systemctl restart systemd-oomd 2>/dev/null || true
        echo "  systemd-oomd pressure-kill policy applied (user.slice kill @60% PSI; per-user manager kill)."
    else
        echo "  systemd-oomd pressure-kill policy already in place."
    fi

    _memres_swap_check
    return 0
}
