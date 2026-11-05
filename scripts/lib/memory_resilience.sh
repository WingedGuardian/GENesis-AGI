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
# install.sh/bootstrap.sh/update.sh under `set -e`.
#
# Sourced by scripts/install.sh + scripts/bootstrap.sh (fresh installs) and
# scripts/update.sh (existing installs retrofit on their next update).
# Idempotent: unchanged
# drop-ins are left untouched and produce no systemd churn.
#
# Test seams (defaults are the real system paths; pytest overrides these and
# stubs sudo/systemctl/swapon/systemd-detect-virt on PATH):
MEMRES_ETC_ROOT="${MEMRES_ETC_ROOT:-/etc}"
MEMRES_SYSTEMD_RUNTIME_DIR="${MEMRES_SYSTEMD_RUNTIME_DIR:-/run/systemd/system}"
MEMRES_PSI_FILE="${MEMRES_PSI_FILE:-/proc/pressure/memory}"
MEMRES_CGROUP_SWAP_MAX="${MEMRES_CGROUP_SWAP_MAX:-/sys/fs/cgroup/memory.swap.max}"
MEMRES_USER_UNIT_DIR="${MEMRES_USER_UNIT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}"
MEMRES_OOMCTL_POLL_S="${MEMRES_OOMCTL_POLL_S:-1}"
MEMRES_OOMCTL_CONFIRM_S="${MEMRES_OOMCTL_CONFIRM_S:-2}"

# The pressure policy. Where oomd watches decides what it may kill: only leaf
# cgroups below a monitored unit are candidates, one per action, ranked by
# preference (avoid last, omit never) and then by reclaim rate — and the
# preference marks are honoured only when the candidate and the monitored
# cgroup have the same owner (systemd.resource-control(5)).
#
# - The operative monitor is the per-user manager (user@<uid>.service), owned by
#   the user, so genesis-server's `avoid` and the tmux server scope's `omit`
#   (scripts/slot_exec.sh --omit) hold.
#   PID 1 reports it from the user@.service drop-in below, but a
#   `systemctl --user daemon-reload` silently ERASES that report (systemd issue
#   #43174): the user manager's own root slice `-.slice` has the same cgroup
#   path and reports `auto` for it. So the user manager reports `kill` for that
#   path itself, from a drop-in on its root slice, and both reports carry the
#   same limit (oomd keeps the last one received).
# - user.slice (root-owned, so the preference marks are ignored under it) stays
#   monitored as the backstop at a HIGHER limit, 80%. The user-manager monitor
#   at 60% acts first; the backstop fires only on pressure it could not relieve:
#   a hog outside the user manager (an SSH build in a login scope) or a runaway
#   in a unit the first monitor ranks last (genesis-server). It is never turned
#   off, so no state of this setup leaves oomd monitoring nothing.
# - init.scope (the user manager itself) is omitted and dbus avoided: killing
#   either takes every session down with it.
# SwapUsedLimit only acts where oomd can see swap (bare/VM installs; inside a
# container the host owns swap and the pressure path is the active mechanism).
_MEMRES_USER_SLICE_CONF=$'[Slice]\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=80%'
_MEMRES_USER_SERVICE_CONF=$'[Service]\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=60%'
_MEMRES_OOMD_CONF=$'[OOM]\nSwapUsedLimit=90%\nDefaultMemoryPressureLimit=60%\nDefaultMemoryPressureDurationSec=20s'
_MEMRES_UM_ROOT_CONF=$'[Slice]\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=60%'
# Earlier Genesis contents, recognised as Genesis-written (see _memres_install_dropin).
_MEMRES_USER_SERVICE_CONF_V1=$'[Service]\nManagedOOMMemoryPressure=kill'
_MEMRES_USER_SLICE_CONF_V1=$'[Slice]\nManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressureLimit=60%'
_MEMRES_UM_INIT_CONF=$'[Scope]\nManagedOOMPreference=omit'
_MEMRES_UM_DBUS_CONF=$'[Service]\nManagedOOMPreference=avoid'

# The write step is shared with every other /etc file Genesis manages
# (scripts/lib/managed_etc.sh): stamped, and a drop-in edited by hand is KEPT and
# named rather than overwritten. Sourced relative to this file, so the manual
# `sudo bash -c 'source scripts/lib/memory_resilience.sh && …'` keeps working.
# shellcheck source=managed_etc.sh
# Pure parameter expansion, not dirname: callers may run with a minimal PATH.
_MEMRES_LIB_DIR="${BASH_SOURCE[0]%/*}"
[ "$_MEMRES_LIB_DIR" = "${BASH_SOURCE[0]}" ] && _MEMRES_LIB_DIR=.
. "$_MEMRES_LIB_DIR/managed_etc.sh"

# _memres_install_dropin <abs-path> <content> — write-if-different via sudo.
# Sets _MEMRES_CHANGED=1 when the content changed (a variable, NOT stdout —
# capturing stdout as the change flag would let any future diagnostic echo
# masquerade as "changed" and churn systemd on idempotent runs), and
# _MEMRES_RELOAD=1 when only the stamp line was added (daemon-reload, no oomd
# restart). A failed write WARNS instead of falling through to "already in
# place". Never fails the caller.
_memres_install_dropin() {
    # Extra arguments are EARLIER Genesis contents of this file. A drop-in still
    # holding one unstamped (written before stamps, never updated since) is
    # Genesis's own, not a hand edit: stamp it as that content first, which
    # genesis_etc_put treats as a migration, so the write below replaces it
    # instead of keeping it as "edited". The first content change to these
    # files is what makes this necessary (managed_etc.sh header).
    local path="$1" content="$2" old
    shift 2
    local snap=""
    if [ "$#" -gt 0 ] && snap="$(mktemp)"; then
        # shellcheck disable=SC2024  # sudo reads the root file; the temp file it writes to is ours
        sudo cat "$path" > "$snap" 2>/dev/null || : > "$snap"
        for old in "$@"; do
            if [ -s "$snap" ] && _genesis_etc_equal "$snap" "$old"; then
                genesis_etc_put "$path" "$old" "" dropin
                break
            fi
        done
        rm -f -- "$snap"
    fi
    genesis_etc_put "$path" "$content" "" dropin
    case "$GENESIS_ETC_RESULT" in
        written|taken) _MEMRES_CHANGED=1 ;;
        migrated) _MEMRES_RELOAD=1 ;;
        kept-edited) _MEMRES_KEPT=1 ;;
        kept-nonregular|uninspected|failed) _MEMRES_FAILED=1 ;;
    esac
    return 0
}

# _memres_user_put <abs-path> <content> — write a user-manager drop-in the way
# genesis_etc_put writes an /etc one (same stamp, scripts/lib/managed_units.py),
# without sudo: the user owns these files. Sets _MEMRES_USER_RESULT to written,
# migrated, unchanged, kept-edited, kept-nonregular or failed. Never fails.
_memres_user_put() {
    local path="$1" content stamped state tmp
    content="$(printf '%s' "$2")"
    _MEMRES_USER_RESULT="failed"
    if [ -L "$path" ] || { [ -e "$path" ] && [ ! -f "$path" ]; }; then
        echo "  Kept: $path is a symlink or not a regular file; not writing through it."
        _MEMRES_USER_RESULT="kept-nonregular"
        return 0
    fi
    if ! stamped="$(printf '%s\n' "$content" | python3 -I -S "$_GENESIS_ETC_MU" stamp)"; then
        echo "  WARNING: could not stamp the content for $path; not written."
        return 0
    fi
    local result="written"
    if [ -f "$path" ]; then
        if _genesis_etc_equal "$path" "$stamped"; then
            _MEMRES_USER_RESULT="unchanged"
            return 0
        fi
        if _genesis_etc_equal "$path" "$content"; then
            result="migrated"
        else
            state="$(python3 -I -S "$_GENESIS_ETC_MU" verify < "$path" 2>/dev/null)" || state=""
            if [ "$state" != "stamped" ]; then
                echo "  Kept: $path was edited by hand. Not overwritten; delete it to get the Genesis version."
                _MEMRES_USER_RESULT="kept-edited"
                return 0
            fi
        fi
    fi
    tmp="${path%/*}/.genesis-tmp.$$"
    if ! mkdir -p "${path%/*}" 2>/dev/null \
        || ! printf '%s\n' "$stamped" > "$tmp" 2>/dev/null \
        || ! mv -f -- "$tmp" "$path" 2>/dev/null; then
        rm -f -- "$tmp" 2>/dev/null
        echo "  WARNING: could not write $path."
        return 0
    fi
    _MEMRES_USER_RESULT="$result"
    return 0
}

# _memres_oomd_monitors <cgroup path> — exit 0 when `oomctl` lists the path
# under "Memory Pressure Monitored CGroups".
_memres_oomd_monitors() {
    command -v oomctl >/dev/null 2>&1 || return 1
    oomctl 2>/dev/null | awk -v want="$1" '
        /^Memory Pressure Monitored CGroups:/ { inside = 1; next }
        /^[^[:space:]]/ { inside = 0 }
        inside && $1 == "Path:" && $2 == want { found = 1 }
        END { exit !found }'
}

# _memres_user_monitor_apply — the user-level half: no sudo, run as the user.
# Writes the user-manager drop-ins, reloads the user manager when one changed,
# and sets _MEMRES_UM_OK=1 only when the monitor is verified LIVE: the drop-in
# is the Genesis one, the user manager reports `kill` for its root slice, and
# oomctl lists that cgroup on two reads apart. oomctl alone cannot tell PID 1's
# report from the user manager's (one path, last message wins), so the property
# read is what proves the user manager's own report, the one that survives a
# reload.
_memres_user_monitor_apply() {
    _MEMRES_UM_OK=0
    _MEMRES_UM_NOTE=""
    if [ "$(id -u)" = "0" ]; then
        _MEMRES_UM_NOTE="user-manager monitor not applied: running as root (run bootstrap.sh as the Genesis user)"
        echo "  Skipped the user-manager monitor: $_MEMRES_UM_NOTE."
        return 0
    fi
    local root_result init_result dbus_result changed=0 reload_rc=0 cg mode limit pref i r
    _memres_user_put "$MEMRES_USER_UNIT_DIR/-.slice.d/genesis-oomd.conf" "$_MEMRES_UM_ROOT_CONF"
    root_result="$_MEMRES_USER_RESULT"
    _memres_user_put "$MEMRES_USER_UNIT_DIR/init.scope.d/genesis-oomd.conf" "$_MEMRES_UM_INIT_CONF"
    init_result="$_MEMRES_USER_RESULT"
    _memres_user_put "$MEMRES_USER_UNIT_DIR/dbus.service.d/genesis-oomd.conf" "$_MEMRES_UM_DBUS_CONF"
    dbus_result="$_MEMRES_USER_RESULT"
    case "$root_result $init_result $dbus_result" in *written*|*migrated*) changed=1 ;; esac
    if [ "$changed" = 1 ]; then
        systemctl --user daemon-reload >/dev/null 2>&1 || reload_rc=$?
    fi
    for r in "root-slice:$root_result" "init.scope:$init_result" "dbus.service:$dbus_result"; do
        case "${r#*:}" in
            written|migrated|unchanged) ;;
            *) _MEMRES_UM_NOTE="its ${r%%:*} drop-in was ${r#*:}"
               echo "  User-manager monitor not verified: $_MEMRES_UM_NOTE."
               return 0 ;;
        esac
    done
    if [ "$reload_rc" != 0 ]; then
        _MEMRES_UM_NOTE="systemctl --user daemon-reload failed (exit $reload_rc)"
        echo "  User-manager monitor not verified: $_MEMRES_UM_NOTE."
        return 0
    fi
    mode="$(systemctl --user show -p ManagedOOMMemoryPressure --value -- -.slice 2>/dev/null)" || mode=""
    if [ "$mode" != "kill" ] && [ "$changed" = 0 ]; then
        # The drop-in is in place but the manager does not report it: a reload
        # on an earlier run failed or never happened. Reload now.
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        mode="$(systemctl --user show -p ManagedOOMMemoryPressure --value -- -.slice 2>/dev/null)" || mode=""
    fi
    cg="$(systemctl --user show -p ControlGroup --value -- -.slice 2>/dev/null)" || cg=""
    if [ "$mode" != "kill" ] || [ -z "$cg" ]; then
        _MEMRES_UM_NOTE="the user manager reports '${mode:-nothing}' for its root slice"
        echo "  User-manager monitor not verified: $_MEMRES_UM_NOTE."
        return 0
    fi
    # The limit as the manager holds it: a fraction of 2^32 (60% is 2576980377,
    # MEASURED systemd 255). Anything else, including a build that prints a
    # percentage, reads as not verified: an honest miss, never a false live.
    limit="$(systemctl --user show -p ManagedOOMMemoryPressureLimit --value -- -.slice 2>/dev/null)" || limit=""
    case "$limit" in
        2576980377) ;;
        *) _MEMRES_UM_NOTE="the user manager reports limit '${limit:-nothing}' for its root slice, not 60%"
           echo "  User-manager monitor not verified: $_MEMRES_UM_NOTE."
           return 0 ;;
    esac
    for r in "init.scope:omit" "dbus.service:avoid"; do
        pref="$(systemctl --user show -p ManagedOOMPreference --value -- "${r%%:*}" 2>/dev/null)" || pref=""
        if [ "$pref" != "${r#*:}" ]; then
            _MEMRES_UM_NOTE="the user manager reports '${pref:-nothing}' for ${r%%:*}, not ${r#*:}"
            echo "  User-manager monitor not verified: $_MEMRES_UM_NOTE."
            return 0
        fi
    done
    # The report reaches oomd asynchronously after the reload: poll, then
    # confirm on a second read so a report about to be erased is not trusted.
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if _memres_oomd_monitors "$cg"; then
            break
        fi
        if [ "$i" = 10 ]; then
            _MEMRES_UM_NOTE="oomctl does not list $cg"
            echo "  User-manager monitor not verified: $_MEMRES_UM_NOTE."
            return 0
        fi
        sleep "$MEMRES_OOMCTL_POLL_S"
    done
    sleep "$MEMRES_OOMCTL_CONFIRM_S"
    if ! _memres_oomd_monitors "$cg"; then
        _MEMRES_UM_NOTE="oomctl listed $cg, then dropped it"
        echo "  User-manager monitor not verified: $_MEMRES_UM_NOTE."
        return 0
    fi
    _MEMRES_UM_OK=1
    echo "  User-manager monitor live: oomd watches $cg."
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
        echo "  WARNING: cgroup memory.swap.max is 0 — memory spikes will thrash instead of swapping."
        if systemd-detect-virt --container >/dev/null 2>&1; then
            echo "           Fix on the HOST (not from inside this container):"
            echo "             incus config set <container-name> limits.memory.swap true"
            echo "           and ensure the host itself has swap (swapon --show)."
        else
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

# _memres_policy_summary — one line naming the monitors this run left in force.
# Both halves come from reads made now: the user half from
# _memres_user_monitor_apply, the backstop from PID 1's own view of
# user.slice (80% is 3435973836 as a fraction of 2^32, MEASURED systemd 255),
# so a reload that failed on an earlier run is not reported as in force.
_memres_policy_summary() {
    local um bs backstop
    if [ "$_MEMRES_UM_OK" = 1 ]; then
        um="user manager kill @60% PSI, init.scope omit, dbus avoid, verified live"
    else
        um="user manager NOT verified (${_MEMRES_UM_NOTE:-see above})"
    fi
    bs="$(systemctl show -p ManagedOOMMemoryPressure -p ManagedOOMMemoryPressureLimit --value user.slice 2>/dev/null | paste -sd' ')" || bs=""
    case "$bs" in
        "kill 3435973836") backstop="user.slice backstop kill @80% PSI" ;;
        *) backstop="user.slice backstop NOT confirmed (systemd reports '${bs:-nothing}')" ;;
    esac
    if ! systemctl is-active --quiet systemd-oomd 2>/dev/null; then
        backstop="systemd-oomd is not running, so no monitor is in force"
        um=""
    fi
    echo "${um:+$um; }$backstop"
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
    # The user half needs no sudo, so without sudo it runs on its own. With
    # sudo it runs AFTER the /etc half, so its live check sees the oomd that
    # half may have restarted, and the summary describes the final state.
    if ! sudo -n true 2>/dev/null; then
        _memres_user_monitor_apply
        echo "  Skipped the /etc policy: sudo unavailable non-interactively. To apply it manually:"
        echo "           sudo bash -c 'source scripts/lib/memory_resilience.sh && memory_resilience_apply'"
        echo "           (as root the user-manager half is skipped; run bootstrap.sh as the user for it)"
        return 0
    fi

    _MEMRES_CHANGED=""
    _MEMRES_RELOAD=""
    _MEMRES_KEPT=""
    _MEMRES_FAILED=""
    _memres_install_dropin "$MEMRES_ETC_ROOT/systemd/system/user.slice.d/genesis-oomd.conf" "$_MEMRES_USER_SLICE_CONF" "$_MEMRES_USER_SLICE_CONF_V1"
    _memres_install_dropin "$MEMRES_ETC_ROOT/systemd/system/user@.service.d/genesis-oomd.conf" "$_MEMRES_USER_SERVICE_CONF" "$_MEMRES_USER_SERVICE_CONF_V1"
    _memres_install_dropin "$MEMRES_ETC_ROOT/systemd/oomd.conf.d/genesis.conf" "$_MEMRES_OOMD_CONF"

    if [[ -n "$_MEMRES_CHANGED" ]]; then
        # daemon-reload propagates the ManagedOOM* unit properties to oomd;
        # the restart picks up oomd.conf.d (and starts oomd if stopped, so a
        # separate `enable --now` would just double the start transition).
        # Runs even on a partial apply — whatever DID land should take effect.
        # Best-effort: a hiccup here must never abort bootstrap/update — the
        # drop-ins are on disk and the next boot applies them regardless.
        sudo systemctl daemon-reload 2>/dev/null || true
        sudo systemctl enable systemd-oomd 2>/dev/null || true
        sudo systemctl restart systemd-oomd 2>/dev/null || true
    elif [[ -n "$_MEMRES_RELOAD" ]]; then
        sudo systemctl daemon-reload 2>/dev/null || true
    fi
    # After an oomd restart with no user reload, "verified" may rest briefly on
    # PID 1's report (same owner, same policy); the next user reload re-sends
    # the user manager's own.
    _memres_user_monitor_apply
    if [[ -n "$_MEMRES_FAILED" ]]; then
        echo "  systemd-oomd policy NOT fully applied — see warnings above."
    elif [[ -n "$_MEMRES_KEPT" ]]; then
        echo "  systemd-oomd policy: a drop-in edited by hand was kept — see above."
    elif [[ -n "$_MEMRES_CHANGED" ]]; then
        echo "  systemd-oomd pressure-kill policy applied ($(_memres_policy_summary))."
    else
        echo "  systemd-oomd pressure-kill policy already in place ($(_memres_policy_summary))."
    fi

    _memres_swap_check
    return 0
}

# The per-user-slice PID/task ceiling. systemd's stock default is TasksMax=33%
# (user-.slice.d/10-defaults.conf) — a conservative multi-user-server default.
# On a single-user Genesis appliance running many concurrent Claude Code sessions
# (each spawns MCP subprocess trees), 33% of the box's PID budget is exhausted
# well before memory/CPU/disk, producing `Cannot fork` while every other axis
# reads green. We raise it to 60% — a PERCENTAGE, so it self-calibrates to each
# box's own budget — while deliberately RESERVING ~40% for system.slice so a
# runaway in the user slice can never starve sshd/systemd/init (the container
# stays recoverable). A fraction, never an absolute count.
_MEMRES_TASKSMAX_CONF=$'[Slice]\nTasksMax=60%'

# pid_budget_apply — raise the per-user-slice TasksMax above systemd's stock 33%.
# Same provision-or-surface contract as memory_resilience_apply: applies the
# drop-in when sudo is available, else skips with the exact manual remediation
# (the awareness posture check then surfaces the un-raised ceiling via the
# pid_ceiling_effective_ok fact). Idempotent. Always returns 0 — must never abort
# bootstrap.sh/update.sh under `set -e`.
pid_budget_apply() {
    echo "--- PID budget (per-user-slice TasksMax) ---"

    if [[ ! -d "$MEMRES_SYSTEMD_RUNTIME_DIR" ]]; then
        echo "  Skipped: not a systemd system (no $MEMRES_SYSTEMD_RUNTIME_DIR)."
        return 0
    fi
    if ! sudo -n true 2>/dev/null; then
        echo "  Skipped: sudo unavailable non-interactively. To apply manually:"
        echo "           sudo bash -c 'source scripts/lib/memory_resilience.sh && pid_budget_apply'"
        echo "           (the awareness posture check surfaces the un-raised 33% ceiling until then)."
        return 0
    fi

    _MEMRES_CHANGED=""
    _MEMRES_RELOAD=""
    _MEMRES_KEPT=""
    _MEMRES_FAILED=""
    # High-numbered so it wins over systemd's 10-defaults.conf (33%); the
    # user-.slice TEMPLATE dir applies to every per-user slice regardless of UID.
    _memres_install_dropin "$MEMRES_ETC_ROOT/systemd/system/user-.slice.d/90-genesis-tasksmax.conf" "$_MEMRES_TASKSMAX_CONF"

    if [[ -n "$_MEMRES_CHANGED$_MEMRES_RELOAD" ]]; then
        # daemon-reload re-reads unit config; TasksMax applies to an already-
        # running slice on reload and unconditionally at its next (re)start/login.
        # Best-effort: the drop-in on disk is the durable guarantee — a reload
        # hiccup must never abort bootstrap/update.
        sudo systemctl daemon-reload 2>/dev/null || true
    fi
    if [[ -n "$_MEMRES_FAILED" ]]; then
        echo "  TasksMax drop-in NOT applied — see warning above (posture check will surface it)."
    elif [[ -n "$_MEMRES_KEPT" ]]; then
        echo "  TasksMax drop-in edited by hand was kept — see above."
    elif [[ -n "$_MEMRES_CHANGED" ]]; then
        echo "  PID ceiling raised: user-.slice TasksMax=60% (was systemd's 33% default)."
    else
        echo "  PID ceiling already provisioned (user-.slice TasksMax=60%)."
    fi
    return 0
}
