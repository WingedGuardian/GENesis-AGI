# Live-activate container memory swap on a RUNNING incus/LXC container.
# Sourced, not executed (no shebang) — host-setup.sh dot-sources it and its
# caller runs under `set -euo pipefail`, so every step here must be errexit-safe.
#
# incus applies `limits.memory.swap` only when the container STARTS, so on an
# already-running container — including one just created above, and every
# retrofit run of host-setup — the live cgroup keeps `memory.swap.max=0` until
# the next restart. The config is set but silently no-ops meanwhile (observed
# 2026-07: a swap retrofit looked applied while the container stayed one memory
# spike away from OOM-thrash, the exact failure the setting is meant to prevent).
# This mirrors what incus does at start by writing the live cgroup now, so swap
# is active immediately without a disruptive container restart.
#
# Best-effort and idempotent: it does nothing if the cgroup knob is absent
# (container stopped, cgroup v1, or a non-standard layout — the config-set alone
# still applies on the next start) or already non-zero. It never fails its
# caller.
#
# Overridable for tests: CONTSWAP_CGROUP_BASE (default /sys/fs/cgroup); `sudo`
# and `tee` are resolved from PATH.

container_swap_activate_live() {
    local name="$1"
    [ -n "$name" ] || return 0
    local base="${CONTSWAP_CGROUP_BASE:-/sys/fs/cgroup}"
    local swap_max="$base/lxc.payload.$name/memory.swap.max"

    # Absent → container not running, or a cgroup layout we can't address here;
    # the persisted `limits.memory.swap=true` still applies on the next start.
    [ -e "$swap_max" ] || return 0

    # Already permitting swap (non-zero, e.g. "max") → idempotent no-op. The
    # cgroup knob is root-owned, so read it with sudo (host-setup runs as root
    # or with passwordless sudo). The `|| cur=""` keeps a failed read — perm
    # denied, or the container stopping between the -e check and the read — from
    # tripping the caller's `set -e`; an unreadable knob falls through to a safe
    # no-op rather than aborting the whole host-setup run.
    local cur=""
    cur="$(sudo cat "$swap_max" 2>/dev/null)" || cur=""
    [ "$cur" = "0" ] || return 0

    if echo max | sudo tee "$swap_max" >/dev/null 2>&1; then
        echo "  + activated swap live on the running container (memory.swap.max=max)"
    else
        echo "  WARNING: could not write $swap_max live — restart the container to activate swap."
    fi
    return 0
}
