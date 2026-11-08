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

# Capture the self-contained CLI. Its namespace stays separate from this fixed
# bootstrap, so future imports and CLI names cannot change bootstrap dispatch.
_genesis_capture_shared_mcp_source() {
    local root="$1" _shared_mcp_cli
    _shared_mcp_cli="$(cat "$root/scripts/lib/mcp_rollout.py" 2>/dev/null)" &&
        [ -n "$_shared_mcp_cli" ] && printf '%s\n' "$_shared_mcp_cli"
}
_SHARED_MCP_ROLLOUT_PY="$(_genesis_capture_shared_mcp_source "$(dirname -- "${BASH_SOURCE[0]}")/../..")" || _SHARED_MCP_ROLLOUT_PY=""
_SHARED_MCP_BOOTSTRAP_PY="$(cat <<'PY'
import sys

scope = {"__name__": "_captured_mcp_rollout", "__file__": sys.argv[2] + "/scripts/lib/mcp_rollout.py"}
exec(compile(sys.argv[1], "<captured mcp_rollout.py>", "exec"), scope)
main, error_type, say = (scope.get(name) for name in ("main", "RolloutError", "_say"))
if not (callable(main) and isinstance(error_type, type) and issubclass(error_type, Exception) and callable(say)):
    raise RuntimeError("captured rollout lacks its required entry points")
try:
    status = main(["units", "--root", sys.argv[2]])
except error_type as exc:
    say(str(exc))
    raise SystemExit(1)
if type(status) is not int:
    raise RuntimeError("captured rollout main did not return an exit status")
raise SystemExit(status)
PY
)"

genesis_restart_shared_mcp_units() {
    local root="$1" units unit state load enabled active inv_before inv_after rrc rc=0
    if [ -z "$_SHARED_MCP_ROLLOUT_PY" ]; then
        # An absent startup source may arrive with the synchronized checkout.
        # Never replace a nonempty snapshot, even if executing it later fails.
        _SHARED_MCP_ROLLOUT_PY="$(_genesis_capture_shared_mcp_source "$root")" || _SHARED_MCP_ROLLOUT_PY=""
    fi
    if [ -z "$_SHARED_MCP_ROLLOUT_PY" ]; then
        echo "  NOTE: shared MCP CLI source capture is missing or empty; shared MCP units were not checked."
        return 1
    fi
    # stderr stays apart: on success stdout is the unit list and nothing else.
    if ! units="$(python3 -I -S -c "$_SHARED_MCP_BOOTSTRAP_PY" "$_SHARED_MCP_ROLLOUT_PY" "$root")"; then
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
        # systemd prints every requested property, empty when it has none, so a
        # loaded unit with no enablement or activity answer is a partial read:
        # treating it as disabled would leave an enabled service on old code.
        if [ -z "$enabled" ] || [ -z "$active" ]; then
            echo "  WARNING: incomplete state for shared MCP unit $unit (UnitFileState='$enabled' ActiveState='$active'); it was not restarted."
            rc=1
            continue
        fi
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
