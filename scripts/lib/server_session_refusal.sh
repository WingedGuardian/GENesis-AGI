# shellcheck shell=bash
# Shared restart refusal for server-launched Claude Code sessions.
#
# Source at startup: a merge may replace files on disk while a script runs, so
# helper code is read now and the started copy is kept.
#
# The caller provides die, _positive_int, ALLOW_KILLING, and _PORT_PROBE_PY.
# _PORT_PROBE_PY stays in deploy_code_only.sh for its
# GENESIS_DEPLOY_PORT_PROBE test seam and post-restart use.
# GENESIS_DEPLOY_PROC_ROOT is a test seam read below.

# What the server would cancel (GET, internal bearer). The token lives in the
# SERVER's Genesis home (_server_genesis_home).
INFLIGHT_PORT=5000
INFLIGHT_URL="http://127.0.0.1:$INFLIGHT_PORT/api/genesis/inflight"

_SERVER_SESSIONS_PY="$(cat "$(unset CDPATH; cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/server_sessions.py")"

genesis_check_allow_killing_items() {
    # Each item is what a refusal prints: a process as <pid>@<start> (its start time
    # in clock ticks, so a reused pid is not covered) or the id of a piece of work
    # the server reported (the shape scripts/lib/server_sessions.py prints).
    _ak_item='([0-9]+@[0-9]+|[A-Za-z0-9][A-Za-z0-9_.:-]{0,63})'
    [[ "$ALLOW_KILLING" == all || "$ALLOW_KILLING" =~ ^${_ak_item}(,${_ak_item})*$ ]] \
        || die "--allow-killing takes the items a refusal names (<pid>@<start>, or an id the server reported), separated by commas, or all (got: $ALLOW_KILLING)"
}

# Claude Code sessions the server launched (dispatched work, Telegram turns,
# reflections). A restart ends every one of them: on stop the server cancels its
# in-flight work, and a dispatched session's row is marked failed. So a restart
# while any runs is a refusal, naming them, unless --allow-killing covers each
# one. Called under the lock as the last step before the server is touched. Two
# signals (scripts/lib/server_sessions.py; its docstring says why each):
#   - the SERVER'S OWN account of the work a restart would cancel
#     (GET /api/genesis/inflight, with the internal API token): every Claude
#     invocation, plus the whole life of a dispatched session or a CLI
#     reflection, from before its Claude process starts until after its result
#     is delivered. Named by the id the server gives it. NOT covered: what other
#     subsystems do after their invocation returns (#2917). A server that answers
#     404 predates the report (the first deploy of this change); the process scan
#     alone then decides, and says so;
#   - the server's live Claude Code process DESCENDANTS, each named <pid>@<start>
#     so a reused pid is not covered: a check that needs nothing from the server.
# Whatever cannot be read or asked (the MainPID, the process table, the token,
# the server) refuses, unless --allow-killing all.
# GENESIS_DEPLOY_PROC_ROOT is a TEST seam, like GENESIS_DEPLOY_ROOT.
# The Genesis home of the RUNNING server, which keeps its internal API token there:
# the server may take GENESIS_HOME from its unit's EnvironmentFile, which this
# shell never sees, so it is read from the server process (same user: readable).
# Only when that cannot be read does the caller's own GENESIS_HOME stand in; a
# wrong guess finds no token or the wrong one, and refuses.
_server_genesis_home() {  # $1 = MainPID
    local env_file="${GENESIS_DEPLOY_PROC_ROOT:-/proc}/$1/environ" gh h
    if [ ! -r "$env_file" ]; then
        printf '%s' "${GENESIS_HOME:-$HOME/.genesis}"
        return 0
    fi
    # Only the two variables are extracted: the server's environment also holds its
    # API keys, which must never pass through this shell (xtrace would print them).
    gh="$(grep -z -m1 '^GENESIS_HOME=' "$env_file" 2>/dev/null | tr -d '\0' || true)"
    gh="${gh#GENESIS_HOME=}"
    h="$(grep -z -m1 '^HOME=' "$env_file" 2>/dev/null | tr -d '\0' || true)"
    h="${h#HOME=}"
    h="${h:-$HOME}"
    # shellcheck disable=SC2088  # matches a LITERAL ~ in the value, expanded by hand
    case "$gh" in
        "~") gh="$h" ;;
        "~/"*) gh="$h/${gh#\~/}" ;;
    esac
    printf '%s' "${gh:-$h/.genesis}"
}
_refuse_if_sessions() {
    local main found work rc pid age resume self start iid kind label line code resp tok_file
    local unlisted="" all_items="" listing="" own="" item
    _cannot_tell() {
        if [ "$ALLOW_KILLING" = all ]; then
            echo "  WARNING: $1; --allow-killing all given, so proceeding." >&2
            return 0
        fi
        die "$1, so the sessions genesis-server launched cannot be found (a restart would end them) — nothing changed. Pass --allow-killing all to restart anyway."
    }
    if ! main="$(systemctl --user show genesis-server -p MainPID --value 2>/dev/null)"; then
        _cannot_tell "could not read genesis-server's MainPID from systemd"
        return 0
    fi
    if ! _positive_int "${main:-0}"; then
        echo "  No genesis-server process is running, so no session it launched can be ended."
        return 0
    fi
    rc=0
    found="$(python3 -I -S -c "$_SERVER_SESSIONS_PY" "$main" "${GENESIS_DEPLOY_PROC_ROOT:-/proc}" "$$")" || rc=$?
    if [ "$rc" -ne 0 ]; then
        _cannot_tell "could not list processes to find the server's sessions"
        found=""
    fi
    work=""
    tok_file="$(_server_genesis_home "$main")/internal_api_token"
    if ! python3 -I -S -c "$_PORT_PROBE_PY" "$INFLIGHT_PORT" "$main" 2>/dev/null; then
        # Whatever answers on the port must BE the server before it is handed the
        # token or believed: another listener could take the token, or answer 404
        # and pass for a server that predates the report.
        _cannot_tell "could not confirm that genesis-server (pid $main) is what listens on port $INFLIGHT_PORT, so it was not asked what it is running"
    elif [ ! -s "$tok_file" ] || [ ! -r "$tok_file" ]; then
        _cannot_tell "could not read the internal API token ($tok_file) to ask genesis-server what it is running"
    else
        # The token goes straight from its file to curl's stdin (-H @-): never on a
        # command line, and never in a shell variable. -q first, as curl requires:
        # no .curlrc; --noproxy '*': no proxy for loopback.
        resp="$({ printf 'Authorization: Bearer '; head -n1 "$tok_file"; } \
            | curl -q --noproxy '*' -s --max-time 15 -H @- -w '\n%{http_code}' "$INFLIGHT_URL" 2>/dev/null || true)"
        code="${resp##*$'\n'}"
        case "$code" in
            200)
                rc=0
                work="$(printf '%s' "${resp%$'\n'*}" | python3 -I -S -c "$_SERVER_SESSIONS_PY" --inflight)" || rc=$?
                if [ "$rc" -ne 0 ]; then
                    _cannot_tell "genesis-server's in-flight report could not be read"
                    work=""
                fi
                ;;
            404)
                echo "  NOTE: genesis-server predates its in-flight report (HTTP 404), so only the Claude processes below it are checked."
                ;;
            *)
                _cannot_tell "could not ask genesis-server what it is running (HTTP ${code:-no answer})"
                ;;
        esac
    fi
    _consider() {  # $1 = the item an override names ("-" = none can), $2 = its line
        listing+="$2"$'\n'
        if [ "$1" = - ]; then
            [ "$ALLOW_KILLING" = all ] || unlisted+=" (unnamed)"
            return 0
        fi
        all_items+=",$1"
        if [ "$ALLOW_KILLING" != all ] && [[ ",$ALLOW_KILLING," != *",$1,"* ]]; then
            unlisted+=" $1"
        fi
    }
    while IFS=$'\t' read -r pid age resume self start; do
        [ -n "$pid" ] || continue
        item="$pid@$start"
        line="    process $item"
        [ "$age" = -1 ] || line+=", running $((age / 60))m"
        [ "$resume" = - ] || line+=", resumes $resume"
        if [ "$self" = self ]; then
            line+="  <- the session running this command"
            own=1
        fi
        _consider "$item" "$line"
    done <<< "$found"
    while IFS=$'\t' read -r iid kind label age; do
        [ -n "$iid" ] || continue
        if [ "$iid" = more ]; then
            listing+="    ... and $kind more items the server reported"$'\n'
            [ "$ALLOW_KILLING" = all ] || unlisted+=" (more)"
            continue
        fi
        _consider "$iid" "    ${kind:-work} $iid  ${label:-?}, running $((age / 60))m"
    done <<< "$work"
    [ -n "$listing" ] || return 0
    if [ -z "$unlisted" ]; then
        echo "  Ending these sessions with the restart (--allow-killing $ALLOW_KILLING):"
        printf '%s' "$listing"
        return 0
    fi
    {
        echo "ERROR: genesis-server is running Claude Code sessions it launched, and a restart ends them:"
        printf '%s' "$listing"
        echo "  Nothing changed. Wait for them to finish, or pass --allow-killing with what is listed"
        # An item that cannot be named (an unsafe id, or one past the listing cap)
        # can only be covered by `all`: naming the rest would refuse again.
        if [ -n "$all_items" ] && [[ "$unlisted" != *"(unnamed)"* && "$unlisted" != *"(more)"* ]]; then
            echo "  (here: --allow-killing ${all_items#,}) to restart anyway; uncovered now:$unlisted."
        elif [ -n "$all_items" ]; then
            echo "  (some of these cannot be named, so only --allow-killing all covers them) to restart anyway."
        else
            echo "  (none of these can be named: --allow-killing all) to restart anyway."
        fi
        if [ -n "$own" ]; then
            echo "  The session running this command is one of them: a restart ends it too, so hand the"
            echo "  restart to a session the server did not launch rather than overriding."
        else
            echo "  If this was launched detached (systemd-run), it cannot tell whether one of these is"
            echo "  the session that launched it. A session the server started must not restart the"
            echo "  server: hand the restart to one it did not start."
        fi
    } >&2
    exit 1
}
