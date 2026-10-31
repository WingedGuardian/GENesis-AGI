# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# genesis_restart_shared_mcp_units <genesis_root> — restart the shared MCP
# service units after a deploy restarted genesis-server: they import the same
# src/ and would otherwise keep serving the old code to every attached session.
#
# Inert by default. The unit names come from scripts/lib/mcp_rollout.py `units`
# (config/mcp_shared.json plus the user overlay). A unit that is not installed
# (LoadState other than loaded) or not enabled is skipped without a restart, so
# on an install with no shared service this asks systemd a question per name and
# changes nothing. An enabled unit is restarted and must then report active.
#
# Prints one line per unit acted on. Returns 0 when every enabled unit came back
# active (or there were none), 1 when any did not or the config was unreadable;
# the caller decides what that means. Run by the deploy scripts under their
# deploy lock: the restart closes the lock's descriptor when one is named in
# _UPDATE_LOCK_FD, as every other systemctl call there does.
#
# The helper's code is read when this file is sourced, like the deploy scripts'
# other helpers: a merge that replaces it mid-run cannot change this run.

_SHARED_MCP_ROLLOUT_PY="$(cat "$(dirname -- "${BASH_SOURCE[0]}")/mcp_rollout.py" 2>/dev/null || true)"

genesis_restart_shared_mcp_units() {
    local root="$1" units unit state load enabled active inv_before inv_after rrc rc=0
    if [ -z "$_SHARED_MCP_ROLLOUT_PY" ]; then
        echo "  NOTE: scripts/lib/mcp_rollout.py is missing; shared MCP units were not checked."
        return 1
    fi
    # stderr stays apart: on success stdout is the unit list and nothing else.
    if ! units="$(python3 -I -S -c "$_SHARED_MCP_ROLLOUT_PY" units --root "$root")"; then
        echo "  WARNING: cannot read the shared MCP configuration (mcp_shared.json, reason above); no shared MCP unit was restarted."
        return 1
    fi
    while IFS= read -r unit; do
        [ -n "$unit" ] || continue
        state="$(systemctl --user show -p LoadState -p UnitFileState -p ActiveState -p InvocationID -- "$unit" 2>/dev/null || true)"
        load="$(printf '%s\n' "$state" | sed -n 's/^LoadState=//p')"
        enabled="$(printf '%s\n' "$state" | sed -n 's/^UnitFileState=//p')"
        active="$(printf '%s\n' "$state" | sed -n 's/^ActiveState=//p')"
        inv_before="$(printf '%s\n' "$state" | sed -n 's/^InvocationID=//p')"
        # systemd answers LoadState for every name, installed or not, so an empty
        # one means it could not be asked (a busy bus): unknown, not absent.
        if [ -z "$load" ]; then
            echo "  WARNING: cannot read the state of shared MCP unit $unit; it was not restarted."
            rc=1
            continue
        fi
        [ "$load" = loaded ] || continue
        case "$enabled" in enabled|enabled-runtime) ;; *) continue ;; esac
        # A unit someone stopped stays stopped: restart would start it again.
        if [ "$active" = inactive ]; then
            echo "  Shared MCP unit $unit is enabled but stopped; left stopped."
            continue
        fi
        rrc=0
        if [ -n "${_UPDATE_LOCK_FD:-}" ]; then
            systemctl --user restart -- "$unit" {_UPDATE_LOCK_FD}>&- 2>/dev/null || rrc=$?
        else
            systemctl --user restart -- "$unit" 2>/dev/null || rrc=$?
        fi
        inv_after="$(systemctl --user show -p InvocationID --value -- "$unit" 2>/dev/null || true)"
        if [ "$rrc" -eq 0 ] && systemctl --user is-active --quiet -- "$unit" 2>/dev/null \
            && [ -n "$inv_after" ] && { [ -z "$inv_before" ] || [ "$inv_after" != "$inv_before" ]; }; then
            echo "  Restarted shared MCP unit $unit"
        else
            echo "  FAILED: shared MCP unit $unit did not come back on the new code (restart exit $rrc). Converge: systemctl --user restart $unit; check: systemctl --user status $unit"
            rc=1
        fi
    done <<< "$units"
    return "$rc"
}
