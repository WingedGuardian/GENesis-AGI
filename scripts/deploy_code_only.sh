#!/bin/bash
# deploy_code_only.sh — the everyday code-only deploy, serialized with update.sh.
#
# Runtime code is an editable install (the venv's .pth points at src/), so most
# changes need no reinstall: pull main, restart genesis-server. Done by hand that
# takes no lock, so it lands in the middle of another session's validation or of
# update.sh. This script is that path with the discipline update.sh already has.
#
# Usage: scripts/deploy_code_only.sh [deploy|pull|restart|status] [--wait N]
#
#   deploy   (the default) pull main, then restart genesis-server.
#   pull     pull main and sync the git hook copies; NO restart. For a change the
#            tree applies by itself (Claude Code hooks, docs), or to stage code
#            for a later restart. Any range is accepted: the report names every
#            change the running server has not loaded, and the next step.
#   restart  restart genesis-server on the tree as it stands; no fetch.
#   status   read-only, takes no lock: the commit the server booted from, HEAD,
#            and the server's MainPID. This is the validation bracket's reading.
#            Run from a linked worktree, it reports the main checkout.
#   --wait N seconds to queue for the lock (default 900, or
#            GENESIS_DEPLOY_LOCK_WAIT)
#
# Every mode except status runs with:
#   the update.lock, EXCLUSIVE and QUEUING (update.sh keeps `flock -n`, so it
#     REFUSES while a validation holds the lock shared);
#   refusals BEFORE anything changes: a linked worktree, an unfinished update.sh
#     run, a branch other than main, a dirty tree, a unit that runs a different
#     venv, a live foreign deploy marker, and a venv that does not match the
#     pyproject.toml being deployed (that one needs update.sh, which reinstalls);
#   the deploy marker for the whole run (the watchdog defers; a dashboard update
#     or a restore cannot start underneath).
# pull and deploy add a bounded fetch and a fast-forward-only merge. deploy and
# restart add a Guardian pause (no false "Genesis down" alert or paid diagnosis)
# and a health wait sized the way update.sh sizes its own. deploy skips the
# restart when nothing merged and the server already booted from HEAD.
#
# "The commit the server booted from" is read from HEAD's reflog at the unit's
# start time (scripts/lib/serving_commit.py), and is "unknown" whenever the reflog
# cannot prove it. It is the tree at boot: a module the server imports later
# loads whatever is on disk then, so after a pull a server can run a mix.
#
# ON FAILURE: ALERT AND HOLD, no auto-revert (owner decision, 2026-09-06). The
# tree IS the install, so moving it backwards under every live session (hooks,
# scripts and agent definitions resolve from it) is a bigger hazard than a bad
# deploy. A critical alert is queued and the tree is left where it is.
#
# Pending DB migrations apply when the server boots, with no pre-migration
# snapshot — the same as any bare restart (owner ruling, 2026-09-26).
#
# It is long-running (up to the lock wait plus the health window) and restarts
# the server the calling session may depend on, so launch deploy and restart
# DETACHED, as a transient systemd unit, never under a tool timeout or a session's
# background job (both die with the session). Substitute your checkout for
# $HOME/genesis. The unit name carries the time, so a second launch queues on the
# lock instead of failing on a name in use. It needs linger
# (`loginctl show-user "$(id -u)" -p Linger` prints Linger=yes), or the unit dies
# at logout:
#   XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" \
#   DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}" \
#   systemd-run --user --collect --unit "genesis-deploy-code-only-$(date +%s)" \
#     --working-directory="$HOME/genesis" --setenv=PATH="$PATH" \
#     --setenv=SSH_AUTH_SOCK --setenv=GENESIS_HOME \
#     /bin/bash -c 'mkdir -p ~/tmp; exec ./scripts/deploy_code_only.sh restart > ~/tmp/deploy-code-only-$(date +%Y%m%d-%H%M%S).log 2>&1'
#
# Exit codes: 0 done (or nothing to do) · 200 lock wait timed out · 1 any
# refusal or failure (the message says which; refusals change nothing).
#
# Validating against the live server? Hold the lock SHARED for your whole run:
#   flock -s -w 7200 ~/.genesis/locks/update.lock <your command>
# and deploy BEFORE you take it (a deploy inside your own hold waits on itself).
# A daemon your command leaves behind keeps holding the lock. While validators'
# holds overlap, a waiting deploy can starve: flock grants a late shared request
# ahead of a queued exclusive one, so the deploy may run out its --wait.
# The hold stops locked deploys, not every restarter (the watchdog, the
# dashboard's service routes, Guardian recovery) and not a bare `git pull`. So run
# `scripts/deploy_code_only.sh status` at the start and at the end. The run is
# INVALID if, at the start, the server booted from a commit other than HEAD or
# from an unknown one (a pull left code unloaded: restart first), or if at the end
# the booted commit, HEAD or the MainPID differs from the start.

set -euo pipefail

if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "ERROR: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

die() { echo "ERROR: $*" >&2; exit 1; }
# A positive whole number by VALUE: `00` and `000` are zero, whatever they look like.
_positive_int() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$((10#$1))" -gt 0 ]; }

# Every cd below runs with CDPATH unset: with CDPATH set, `cd` resolves a relative
# name against it and prints the directory it chose, which then lands in the
# captured path.
# GENESIS_DEPLOY_ROOT is a TEST seam: tests point it at a fixture tree so no test
# ever restarts the real runtime. Unset, it is the checkout this script lives in.
GENESIS_ROOT="${GENESIS_DEPLOY_ROOT:-$(unset CDPATH; cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV_DIR="${GENESIS_DEPLOY_VENV:-$GENESIS_ROOT/.venv}"
UPDATE_STATE_FILE="$HOME/.genesis/update_state.json"
LOCK_FILE="${GENESIS_HOME:-$HOME/.genesis}/locks/update.lock"
HEALTH_URL="http://localhost:5000/api/genesis/health"
LOCK_HELD_RC=200

# CC sessions lack the D-Bus env `systemctl --user` needs (same guard as update.sh).
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

# Libs load from THIS script's directory: under the test seam the target tree is
# a fixture with no scripts/lib.
_SELF_DIR="$(unset CDPATH; cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/guardian_pause.sh
. "$_SELF_DIR/lib/guardian_pause.sh"
# shellcheck source=lib/deploy_marker.sh
. "$_SELF_DIR/lib/deploy_marker.sh"
# shellcheck source=lib/alert_queue.sh
. "$_SELF_DIR/lib/alert_queue.sh"
# The helpers that run AFTER the merge are read now, like the libs above: the
# merge may replace them on disk, and this run must use the versions it started
# with (`python3 -c "$CODE" args…` sees the same sys.argv as running the file).
# GENESIS_DEPLOY_PORT_PROBE is a TEST seam, like GENESIS_DEPLOY_ROOT: the
# fixture substitutes it so no test ever probes the live server's port.
_PORT_PROBE_PY="$(cat "${GENESIS_DEPLOY_PORT_PROBE:-$_SELF_DIR/lib/port_owned_by.py}")"
_MANIFEST_DELTA_PY="$(cat "$_SELF_DIR/lib/manifest_delta.py")"
_SERVING_COMMIT_PY="$(cat "$_SELF_DIR/lib/serving_commit.py")"

MODE=""
WAIT_S="${GENESIS_DEPLOY_LOCK_WAIT:-900}"
while [ $# -gt 0 ]; do
    case "$1" in
        deploy|pull|restart|status)
            [ -z "$MODE" ] || die "one mode at a time (got '$MODE' and '$1')."
            MODE="$1"; shift ;;
        --wait) WAIT_S="${2:?--wait needs a value}"; shift 2 ;;
        --no-pull) die "--no-pull is now the restart mode: scripts/deploy_code_only.sh restart" ;;
        --no-restart) die "--no-restart is now the pull mode: scripts/deploy_code_only.sh pull" ;;
        *) die "unknown argument: $1 (modes: deploy, pull, restart, status)" ;;
    esac
done
MODE="${MODE:-deploy}"
case "$WAIT_S" in
    ''|*[!0-9]*) die "the lock wait must be a whole number of seconds (got: $WAIT_S)" ;;
esac

# ── A linked worktree is never a deploy target ────────────────────────
# Git answers this; a path test alone misses a worktree added anywhere. The path
# arms stay for a plain clone parked under a worktree directory.
_git_dir="$(git -C "$GENESIS_ROOT" rev-parse --absolute-git-dir 2>/dev/null || true)"
_common_dir="$(unset CDPATH; cd -- "$GENESIS_ROOT" 2>/dev/null && cd -- "$(git rev-parse --git-common-dir 2>/dev/null || echo /nonexistent)" 2>/dev/null && pwd -P || true)"
# status is read-only, and validators usually work in a worktree: from one, it
# reports the main checkout, whose .git is the common dir.
if [ "$MODE" = status ] && [ -n "$_git_dir" ] && [ -n "$_common_dir" ] \
    && [ "$(unset CDPATH; cd -- "$_git_dir" && pwd -P)" != "$_common_dir" ] \
    && [ "$(basename -- "$_common_dir")" = .git ]; then
    GENESIS_ROOT="$(dirname -- "$_common_dir")"
    _git_dir="$_common_dir"
    echo "(from a linked worktree: reporting the main checkout, $GENESIS_ROOT)"
fi
if [ -z "$_git_dir" ] || [ -z "$_common_dir" ] || [ "$(unset CDPATH; cd -- "$_git_dir" && pwd -P)" != "$_common_dir" ] \
    || [[ "$GENESIS_ROOT" == *"/.claude/worktrees/"* ]] || [[ "$GENESIS_ROOT" == *"/.worktrees/"* ]]; then
    die "deploy_code_only.sh must run against the main checkout, not a worktree ($GENESIS_ROOT)."
fi

# ── What the server booted from ───────────────────────────────────────
# Sets SERVING to the commit, or empty with SERVING_WHY saying why it is unknown.
# ActiveState and the start time come from `systemctl show`, never `is-active`,
# and no date is parsed (--timestamp=unix).
_read_serving() {
    local state boot cutoff out rc=0
    SERVING=""
    SERVING_WHY=""
    state="$(systemctl --user show genesis-server -p ActiveState --value 2>/dev/null || true)"
    if [ "$state" != active ]; then
        SERVING_WHY="genesis-server is not running (${state:-state unreadable})"
        return 0
    fi
    boot="$(systemctl --user show genesis-server -p ActiveEnterTimestamp --timestamp=unix --value 2>/dev/null || true)"
    boot="${boot#@}"
    case "$boot" in
        ''|*[!0-9]*)
            SERVING_WHY="cannot read genesis-server's start time in unix seconds (--timestamp=unix needs systemd 248 or newer)"
            return 0 ;;
    esac
    # Before this cutoff git may have expired unreachable reflog entries (a
    # detour, as a pair). git resolves the setting itself; unset means its
    # 30-day default, and a value it cannot read leaves the cutoff empty, which
    # the reader answers as unknown.
    cutoff="$(git -C "$GENESIS_ROOT" config --type=expiry-date gc.reflogExpireUnreachable 2>/dev/null)" || {
        [ "$?" -eq 1 ] && cutoff=$(( $(date +%s) - 30 * 86400 )) || cutoff=""
    }
    out="$(python3 -c "$_SERVING_COMMIT_PY" "$_git_dir/logs/HEAD" "$boot" \
        "$(git -C "$GENESIS_ROOT" rev-parse HEAD)" "$cutoff" 2>/dev/null)" || rc=$?
    if [ "$rc" -eq 0 ] && [ -n "$out" ]; then
        SERVING="$out"
    else
        SERVING_WHY="${out#unknown: }"
        [ -n "$SERVING_WHY" ] || SERVING_WHY="the reflog reader failed"
    fi
}

# The server-loaded changes the running server has not loaded: src/, config/
# (read at startup) and pyproject.toml, between the commit it booted from and
# HEAD. When that commit is unknown, <fallback-from> (this run's pull range) is
# used instead, and said so. Addressed to the session that ran the command: it
# names the next step and pages nobody.
_report_pending() {
    local fallback_from="${1:-}" head from changed
    head="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"
    _read_serving
    if [ -n "$SERVING" ]; then
        echo "  The server booted from $SERVING; the tree is at $head."
        from="$SERVING"
    else
        echo "  The commit the server booted from is unknown: $SERVING_WHY."
        if [ -n "$fallback_from" ] && [ "$fallback_from" != "$head" ]; then
            echo "  Showing what this pull changed instead ($fallback_from..$head)."
            from="$fallback_from"
        else
            return 0
        fi
    fi
    changed="$(git -C "$GENESIS_ROOT" diff --no-renames --name-only "$from" "$head" -- src config pyproject.toml)" \
        || { echo "  NOTE: cannot list the changes since $from."; return 0; }
    if [ -z "$changed" ]; then
        echo "  Nothing the server loads (src/, config/, pyproject.toml) has changed since then."
        return 0
    fi
    echo "  PENDING: the running server has not loaded these changes. It imports src/ lazily,"
    echo "  so until it restarts it can run a mix of old and new code:"
    echo "$changed" | sed 's/^/          /'
    echo "  Next step, once no validation holds the lock: scripts/deploy_code_only.sh restart"
    echo "  (launch it detached; the header of this script has the command)."
}

if [ "$MODE" = status ]; then
    _read_serving
    echo "serving: ${SERVING:-unknown ($SERVING_WHY)}"
    echo "head: $(git -C "$GENESIS_ROOT" rev-parse HEAD)"
    echo "mainpid: $(systemctl --user show genesis-server -p MainPID --value 2>/dev/null || echo unknown)"
    _report_pending
    exit 0
fi

echo ""
echo "  Genesis code-only deploy ($MODE)"
echo "  ────────────────────────"

# ── The lock: exclusive, queuing ──────────────────────────────────────
mkdir -p "$(dirname "$LOCK_FILE")"
exec {_UPDATE_LOCK_FD}>"$LOCK_FILE"
if ! flock -w "$WAIT_S" "$_UPDATE_LOCK_FD"; then
    echo "ERROR: $LOCK_FILE still held after ${WAIT_S}s — update.sh, restore.sh or a" >&2
    echo "       validation hold (flock -s) is running. Nothing changed; retry when it ends." >&2
    exit "$LOCK_HELD_RC"
fi
echo "  Lock held (exclusive): $LOCK_FILE"

# Cleanup arms the moment the lock is ours. It resumes the Guardian and releases
# the marker (each a no-op unless taken), and alerts on a failure that happened
# AFTER the tree moved or the server was touched: the still-running server gives
# no sign of it otherwise.
_PHASE="checks"
_ALERTED=""
_cleanup() {
    local rc=$?
    # A second signal during cleanup must not cut it short (skipping the marker
    # release or the Guardian resume).
    trap '' INT TERM
    if [ "$rc" -ne 0 ] && [ -z "$_ALERTED" ] && [ "$_PHASE" != "checks" ]; then
        local _sha
        _sha="$(git -C "$GENESIS_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
        queue_alert critical deploy-code-only \
            "code-only deploy failed at phase $_PHASE ($_sha)" \
            "The code-only deploy ($MODE) stopped at phase '$_PHASE'. The tree is at $_sha and was NOT reverted (by design). If the merge ran but the restart did not, the running server still has the old code in memory and imports new files lazily. Converge by hand: journalctl --user -u genesis-server -n 50, then scripts/deploy_code_only.sh restart."
    fi
    _guardian_resume
    _release_deploy_marker
}
trap _cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ── Refusals: nothing has changed yet ─────────────────────────────────
# A state file is an unfinished update.sh run UNLESS its phase is "done": update.sh
# writes "done" just before deleting the file, so a run killed in between leaves a
# finished one behind, and the watchdog's reader already treats it as over. Under
# the exclusive lock no update.sh can be running. Anything unreadable refuses.
if [ -e "$UPDATE_STATE_FILE" ]; then
    _state_phase="$(python3 -c 'import json, sys
d = json.load(open(sys.argv[1]))
p = d.get("phase") if isinstance(d, dict) else None
print(p if isinstance(p, str) else "")' "$UPDATE_STATE_FILE" 2>/dev/null || true)"
    [ "$_state_phase" = "done" ] \
        || die "$UPDATE_STATE_FILE records an unfinished update.sh run; finish it with scripts/update.sh --post-merge."
fi
_branch="$(git -C "$GENESIS_ROOT" symbolic-ref --short -q HEAD || true)"
[ "$_branch" = main ] || die "$GENESIS_ROOT is on '${_branch:-a detached HEAD}', not main."
# The status is read on its own first: in a pipeline its failure would be
# swallowed, and an unreadable status would pass as a clean tree.
_status="$(git -C "$GENESIS_ROOT" status --porcelain)" \
    || die "cannot read the working tree's status — nothing was deployed."
_dirty="$(printf '%s\n' "$_status" | grep -v '^??' | grep -vE "$EPHEMERAL_DIRTY_RE" | grep -v '^$' || true)"
if [ -n "$_dirty" ]; then
    echo "ERROR: $GENESIS_ROOT has uncommitted tracked changes. Nothing was deployed:" >&2
    echo "$_dirty" >&2
    exit 1
fi
# The dependency gate asks $VENV_DIR, so the unit must RUN $VENV_DIR: its python
# path is fixed in the unit file at install time and need not be this checkout's
# .venv. Compared as directories, never by resolving the interpreter (a venv's
# python is a symlink to the base one). Unreadable refuses. bootstrap renders the
# unit for this checkout's .venv, and `update.sh --post-merge` runs bootstrap
# whether or not there is anything to merge.
_unit_python="$(systemctl --user show genesis-server -p ExecStart --value 2>/dev/null \
    | sed -n 's/^{ path=\([^ ;]*\) .*$/\1/p' | head -n 1 || true)"
[ -n "$_unit_python" ] || die "cannot read which python genesis-server runs (systemctl show ExecStart)."
[ "$(realpath -m "$(dirname "$_unit_python")")" = "$(realpath -m "$VENV_DIR/bin")" ] \
    || die "genesis-server runs $_unit_python, not $VENV_DIR — run scripts/update.sh --post-merge, whose bootstrap renders the unit for this checkout's venv."
# The deploy marker is taken NOW, before any pull, in every mode: a live foreign
# holder (a dashboard update between its tiers, a restore) is refused before the
# tree moves, and nothing can take the marker between our check and our restart.
# Released by _cleanup.
_marker_rc=0
_acquire_deploy_marker || _marker_rc=$?
if [ "$_marker_rc" -eq 2 ]; then
    die "cannot write the deploy marker $DEPLOY_MARKER_FILE — without it the watchdog could restart genesis-server mid-deploy."
elif [ "$_marker_rc" -ne 0 ]; then
    die "a live deploy (pid $DEPLOY_MARKER_HOLDER) holds $DEPLOY_MARKER_FILE — a dashboard update or a restore is running."
fi

# Does the venv match the pyproject.toml read from stdin (<what> names that
# tree)? A code-only deploy never reinstalls, so any difference goes to the
# command in <remedy>, which does. Exit 3 is the one difference a reinstall
# cannot fix: this interpreter is older than the incoming requires-python.
_deps_ok() {
    local what="$1" remedy="$2" out rc=0
    out="$("$VENV_DIR/bin/python" "$_SELF_DIR/lib/venv_matches_pyproject.py" "$GENESIS_ROOT" 2>&1)" || rc=$?
    case "$rc" in
        0) return 0 ;;
        3) echo "ERROR: $what needs a newer Python than this venv runs, and a reinstall cannot change that:" >&2 ;;
        *) echo "ERROR: the venv does not match $what's pyproject.toml — run $remedy instead:" >&2 ;;
    esac
    echo "$out" | sed 's/^/         /' >&2
    return 1
}

# _deploy_health_paths <NAME> <ref>: a path list the repo's own deploy-health
# snapshot keeps at <ref> (read by AST, no import), never re-listed here. Prints
# one path per line; nothing if unreadable.
_deploy_health_paths() {
    local name="$1" ref="$2" src
    src="$(git -C "$GENESIS_ROOT" show "$ref:src/genesis/observability/snapshots/deploy_health.py" 2>/dev/null)" || return 0
    DH_SRC="$src" "$VENV_DIR/bin/python" -c '
import ast, os, sys
for node in ast.parse(os.environ["DH_SRC"]).body:
    if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == sys.argv[1] for t in node.targets):
        print("\n".join(ast.literal_eval(node.value)))
' "$name" 2>/dev/null || true
}

# What the tmp watchgod runs from the tree at <ref>: its script and the libs it
# sources, read from its own `source` lines rather than listed here.
_watchgod_paths() {
    local src
    src="$(git -C "$GENESIS_ROOT" show "$1:scripts/tmp_watchgod.sh" 2>/dev/null)" || return 0
    echo scripts/tmp_watchgod.sh
    printf '%s\n' "$src" | grep -oE '\$_SCRIPT_DIR/lib/[A-Za-z0-9_]+\.sh' | sed 's#^\$_SCRIPT_DIR/#scripts/#' || true
}

# Each list is the union of its pre-merge and incoming versions: the running
# processes loaded the old list, and the next ones load the new.
_both_refs() {
    { "$@" "$_head"; "$@" "$_upstream"; } | sort -u
}

# ── Pull ──────────────────────────────────────────────────────────────
# The pull for <branch>: a bounded fetch of that branch's configured upstream,
# the dependency gate on the INCOMING pyproject.toml, then a fast-forward. The
# branch is an argument so that another way of advancing the tree can sit
# beside this one, dispatched on the branch, without touching the lock, marker,
# restart or health sections. Sets _PULL_FROM to HEAD before the merge.
_pull() {
    local branch="$1"
    _fetch_timeout="${GENESIS_DEPLOY_FETCH_TIMEOUT:-120}"
    # `timeout 0` means NO limit, and so does `timeout 00`: judge the VALUE, never
    # the spelling.
    _positive_int "$_fetch_timeout" \
        || die "GENESIS_DEPLOY_FETCH_TIMEOUT must be a positive number of seconds (got: $_fetch_timeout)"
    _fetch_timeout=$((10#$_fetch_timeout))
    echo "  Fetching (bounded, ${_fetch_timeout}s)…"
    # Every git that can outlive this script (an auto-gc) runs in the foreground
    # and without the lock fd.
    # Only the branch's configured upstream: a bare `git fetch` pulls every branch on the
    # remote (dozens of PR branches), all of it while the exclusive lock is held.
    _remote="$(git -C "$GENESIS_ROOT" config --get "branch.$branch.remote" || true)"
    _merge_ref="$(git -C "$GENESIS_ROOT" config --get "branch.$branch.merge" || true)"
    [ -n "$_remote" ] && [ -n "$_merge_ref" ] || die "$branch has no upstream to pull from."
    timeout -k 10 "$_fetch_timeout" git -c gc.autoDetach=false -C "$GENESIS_ROOT" fetch -q "$_remote" "$_merge_ref" {_UPDATE_LOCK_FD}>&- \
        || die "fetch failed or timed out — nothing changed."
    _upstream="$(git -C "$GENESIS_ROOT" rev-parse --verify -q "$branch@{u}" || true)"
    [ -n "$_upstream" ] || die "$branch has no upstream to pull from."
    _head="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"
    _PULL_FROM="$_head"
    if [ "$_head" = "$_upstream" ]; then
        echo "  Already at the upstream tip."
        # Nothing to merge, so plain update.sh would stop at "Already up to date"
        # without reinstalling (unless update.sh-only paths changed since its
        # last recorded run); --post-merge always reinstalls, without merging.
        _deps_ok "the working tree" "scripts/update.sh --post-merge" < "$GENESIS_ROOT/pyproject.toml" || exit 1
        return 0
    fi
    git -C "$GENESIS_ROOT" merge-base --is-ancestor "$_head" "$_upstream" \
        || die "$branch has diverged from its upstream — nothing changed; reconcile by hand."
    # The dependency gate reads the INCOMING pyproject.toml, before the merge, so
    # a refusal leaves the tree untouched. update.sh merges it and reinstalls.
    git -C "$GENESIS_ROOT" show "$_upstream:pyproject.toml" | _deps_ok "$_upstream" "scripts/update.sh" \
        || exit 1
    # --no-renames: with rename detection on (git's default), a file MOVED out of
    # a path lists only its destination.
    _range_changed() { git -C "$GENESIS_ROOT" diff --no-renames --name-only "$_head" "$_upstream" -- "$@"; }
    # Excused dirty files that the incoming range ALSO changes. The regenerable
    # ones are cleared, exactly as update.sh does before its merge (they rewrite
    # themselves); otherwise the merge would abort on them every time. The
    # transitional ones hold live install-local data that update.sh backs up
    # first, so a range touching them is update.sh's job.
    for _f in .claude/settings.local.json .serena/project.yml src/genesis/identity/USER.md; do
        if ! git -C "$GENESIS_ROOT" diff --quiet HEAD -- "$_f" 2>/dev/null && [ -n "$(_range_changed "$_f")" ]; then
            die "$_f is edited locally and changed upstream — run scripts/update.sh, which carries it across."
        fi
    done
    # Advisory: what this deploy does NOT activate. scripts/hooks is on the
    # snapshot's list but is applied here: the tree runs the Claude Code hooks,
    # and the git hook copies are synced after the merge.
    mapfile -t _tier2 < <(_both_refs _deploy_health_paths TIER2_PATHS | grep -vx 'scripts/hooks' || true)
    mapfile -t _guardian < <(_both_refs _deploy_health_paths GUARDIAN_HOST_PATHS)
    mapfile -t _watchgod < <(_both_refs _watchgod_paths)
    if [ "${#_tier2[@]}" -eq 0 ] || [ "${#_guardian[@]}" -eq 0 ]; then
        echo "  NOTE: cannot read the deploy-health path lists at either end of this range;"
        echo "        the notes on activation paths and host Guardian code are skipped."
    fi
    if [ "${#_tier2[@]}" -gt 0 ]; then
        _activation="$(_range_changed "${_tier2[@]}" || true)"
        if [ -n "$_activation" ]; then
            echo "  NOTE: this range changes activation paths a code-only deploy does not apply"
            echo "        (systemd units, the bootstrap, the Claude Code pin, dependencies). Run scripts/update.sh for those:"
            echo "$_activation" | sed 's/^/          /'
        fi
    fi
    if [ "${#_guardian[@]}" -gt 0 ]; then
        _host="$(_range_changed "${_guardian[@]}" || true)"
        if [ -n "$_host" ]; then
            echo "  NOTE: this range changes code the host Guardian runs; the host keeps the old copy"
            echo "        until scripts/update.sh redeploys it:"
            echo "$_host" | sed 's/^/          /'
        fi
    fi
    if [ "${#_watchgod[@]}" -gt 0 ]; then
        _wg="$(_range_changed "${_watchgod[@]}" || true)"
        if [ -n "$_wg" ]; then
            echo "  NOTE: this range changes code genesis-tmp-watchgod runs from the tree; the running"
            echo "        watchgod keeps its old copy until it restarts (scripts/update.sh restarts it):"
            echo "$_wg" | sed 's/^/          /'
        fi
    fi
    _PHASE="merging"
    for _f in AGENTS.md config/procedure_triggers.yaml; do
        if git -C "$GENESIS_ROOT" ls-files --error-unmatch "$_f" >/dev/null 2>&1 \
            && ! git -C "$GENESIS_ROOT" diff --quiet HEAD -- "$_f" 2>/dev/null \
            && [ -n "$(_range_changed "$_f")" ]; then
            git -C "$GENESIS_ROOT" checkout HEAD -- "$_f"
        fi
    done
    # Safe for this script to merge the tree it runs from: git REPLACES a changed
    # file rather than rewriting it in place, so bash keeps reading the old copy
    # (measured, git 2.43). update.sh copies itself to temp instead.
    # A merge git refuses (an untracked file in the way, say) leaves the tree
    # where it was: that is a refusal like the others, not a failure to alert on.
    # "Where it was" means HEAD AND the working tree's status, so a merge that
    # failed partway still alerts.
    _status_before="$(git -C "$GENESIS_ROOT" status --porcelain 2>/dev/null)" || _status_before="unreadable before"
    if ! git -c gc.autoDetach=false -C "$GENESIS_ROOT" merge --ff-only -q "$_upstream" {_UPDATE_LOCK_FD}>&-; then
        _status_after="$(git -C "$GENESIS_ROOT" status --porcelain 2>/dev/null)" || _status_after="unreadable after"
        if [ "$(git -C "$GENESIS_ROOT" rev-parse HEAD 2>/dev/null)" = "$_head" ] \
            && [ "$_status_after" = "$_status_before" ]; then
            _PHASE="checks"
            die "git refused the fast-forward to $_upstream (see above) — nothing changed."
        fi
        exit 1
    fi
    _PHASE="merged"
    echo "  Merged $_head..$_upstream"
}

# The installed git hook copies follow the tree (Claude Code hooks run from the
# tree and need nothing). sync-hooks.sh is idempotent and never overwrites a hook
# someone modified; its non-zero exits are reported, never fatal.
_sync_git_hooks() {
    local s="$GENESIS_ROOT/scripts/hooks/sync-hooks.sh" rc=0
    if [ ! -f "$s" ]; then
        echo "  NOTE: $s is missing; the git hook copies were not synced."
        return 0
    fi
    bash "$s" --quiet {_UPDATE_LOCK_FD}>&- || rc=$?
    case "$rc" in
        0) echo "  Git hook copies in sync." ;;
        2) echo "  NOTE: sync-hooks.sh left a user-modified git hook alone (exit 2)." ;;
        *) echo "  NOTE: sync-hooks.sh could not sync the git hooks (exit $rc); they are as they were." ;;
    esac
}

case "$MODE" in
    deploy|pull)
        _pull "$_branch"
        _sync_git_hooks
        ;;
    restart)
        # No fetch, so nothing to merge: update.sh --post-merge is what reinstalls.
        _deps_ok "the working tree" "scripts/update.sh --post-merge" < "$GENESIS_ROOT/pyproject.toml" || exit 1
        ;;
esac
SHA="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"

if [ "$MODE" = pull ]; then
    _report_pending "$_PULL_FROM"
    echo "  Pulled — tree at $SHA (no restart)."
    exit 0
fi

# ── Restart ───────────────────────────────────────────────────────────
# The pre-restart identity and manifest, read while the old server is ALIVE (a
# stopped unit reports MainPID 0, and its manifest then belongs to nobody). They
# are what makes the health check below about the RESTARTED unit rather than
# about whatever answers on the port, and the baseline for the subsystem delta.
_SERVER_PID_BEFORE="$(systemctl --user show genesis-server -p MainPID --value 2>/dev/null || true)"
_MANIFEST_BEFORE="$(cat "$HOME/.genesis/bootstrap_manifest.json" 2>/dev/null || true)"
_read_serving
echo "  The server booted from ${SERVING:-an unknown commit ($SERVING_WHY)}."
# A deploy with nothing to deploy: nothing merged, and the server provably booted
# from HEAD. A restart would only cost an outage and end in-flight dispatched
# sessions. The restart mode is there to force one.
if [ "$MODE" = deploy ] && [ "$_PHASE" != merged ] && [ "$SERVING" = "$SHA" ]; then
    echo "  Nothing to deploy: the server already booted from $SHA."
    exit 0
fi
# The reflog and the unit's start time both count whole seconds, and the boot
# commit is only readable when no move of HEAD shares the boot's second. A second
# of wait puts this boot clear of the merge above.
sleep 1
_guardian_pause
_PHASE="restarting"
echo "  Restarting genesis-server at $SHA…"
systemctl --user restart genesis-server {_UPDATE_LOCK_FD}>&-
_PHASE="restarted"

# Is the RESTARTED unit the one serving? A 200 from the port alone cannot say: a
# server started outside systemd (update.sh's nohup fallback) can keep answering
# while the new unit exits on the process lock it holds. And the bootstrap
# manifest cannot say either: it is written BEFORE the web server binds, and Flask
# runs in a daemon thread, so a failed bind leaves the process up with its
# manifest while something else answers. So the proof is the socket itself: the
# unit is active with a NEW, nonzero MainPID, and every socket listening on the
# health port is one of that pid's own descriptors (scripts/lib/port_owned_by.py,
# which reads /proc and answers no whenever it cannot tell; its code was read at
# startup). Prints the pid.
_HEALTH_PORT=5000
_restarted_unit_serving() {
    local state pid
    state="$(systemctl --user is-active genesis-server 2>/dev/null || true)"
    [ "$state" = active ] || return 1
    pid="$(systemctl --user show genesis-server -p MainPID --value 2>/dev/null || true)"
    [ -n "$pid" ] && [ "$pid" != 0 ] && [ "$pid" != "$_SERVER_PID_BEFORE" ] || return 1
    python3 -c "$_PORT_PROBE_PY" "$_HEALTH_PORT" "$pid" 2>/dev/null || return 1
    printf '%s\n' "$pid"
}

# Health window: the SAME value update.sh computes from the same inputs (pinned by
# a parity test). It is bounded by the Guardian's cover, derived from the lib's
# constants so the two cannot drift apart.
HEALTH_GUARDIAN_COVER=$(( GUARDIAN_PAUSE_RENEW_MAX * (GUARDIAN_PAUSE_TTL / 2) + GUARDIAN_PAUSE_TTL ))
HEALTH_WINDOW_MAX=$(( HEALTH_GUARDIAN_COVER / 2 ))
HEALTH_WINDOW_SECS="${GENESIS_DEPLOY_HEALTH_WINDOW_SECS:-900}"
case "$HEALTH_WINDOW_SECS" in
    ''|*[!0-9]*) HEALTH_WINDOW_SECS=900 ;;
esac
HEALTH_WINDOW_SECS="${HEALTH_WINDOW_SECS#"${HEALTH_WINDOW_SECS%%[!0]*}"}"
[ -z "$HEALTH_WINDOW_SECS" ] && HEALTH_WINDOW_SECS=0
[ "${#HEALTH_WINDOW_SECS}" -gt 6 ] && HEALTH_WINDOW_SECS="$HEALTH_WINDOW_MAX"
HEALTH_WINDOW_SECS=$((10#$HEALTH_WINDOW_SECS))
[ "$HEALTH_WINDOW_SECS" -lt 180 ] && HEALTH_WINDOW_SECS=180
[ "$HEALTH_WINDOW_SECS" -gt "$HEALTH_WINDOW_MAX" ] && HEALTH_WINDOW_SECS="$HEALTH_WINDOW_MAX"

# Monotonic clock (a stepped wall clock would stretch or cut the wait), with the
# clock DOMAIN chosen once: re-deciding per read would compare an epoch against
# an uptime deadline after one failed read and end the wait at once. A failed
# read in the chosen domain returns 0, which holds the wait; the attempt cap is
# the clock-independent backstop. The poll interval is a test seam; 15s is policy.
_poll="${GENESIS_DEPLOY_HEALTH_POLL:-15}"
if _positive_int "$_poll"; then _poll=$((10#$_poll)); else _poll=15; fi
if read -r _ < /proc/uptime 2>/dev/null; then _clock=uptime; else _clock=wall; fi
_now() {
    local up
    if [ "$_clock" = wall ]; then date +%s
    elif read -r up _ < /proc/uptime 2>/dev/null && [ -n "${up%%.*}" ]; then printf '%s' "${up%%.*}"
    else printf '0'
    fi
}
_start="$(_now)"
if [ "$_start" -eq 0 ]; then _clock=wall; _start="$(_now)"; fi
_deadline=$(( _start + HEALTH_WINDOW_SECS ))
_max_attempts=$(( HEALTH_WINDOW_SECS / _poll + 2 ))
_healthy=false
_answered_by_other=false
_SERVER_PID=""
_attempt=0
while [ "$_attempt" -lt "$_max_attempts" ] && [ "$(_now)" -lt "$_deadline" ]; do
    _attempt=$((_attempt + 1))
    sleep "$_poll"
    _seen="not answering yet"
    if curl -sf --max-time 20 "$HEALTH_URL" >/dev/null 2>&1; then
        if _SERVER_PID="$(_restarted_unit_serving)"; then
            _healthy=true
            break
        fi
        # Something answers, but it is not (yet) the restarted unit: still
        # bootstrapping, or an old server outside systemd holding the port.
        _answered_by_other=true
        _seen="the port answers, but not from the restarted unit"
    fi
    # An empty state (systemd/D-Bus busy) is unreadable, not dead: keep waiting.
    _state="$(systemctl --user is-active genesis-server 2>/dev/null || true)"
    case "$_state" in
        active|activating|reloading|'') echo "  Attempt $_attempt: $_seen (unit: ${_state:-unreadable})…" ;;
        *) echo "  Attempt $_attempt: unit is '$_state' — it will not come up on its own."; break ;;
    esac
done

if [ "$_healthy" = true ]; then
    # Which subsystems regressed across the restart? The same check update.sh
    # runs, against the baseline read before the restart. ADVISORY, as there:
    # an otherwise-good deploy is not failed over one non-critical subsystem, but
    # the regression is surfaced rather than swallowed.
    _degraded="$(SERVER_PID="$_SERVER_PID" SERVER_PID_BEFORE="$_SERVER_PID_BEFORE" \
        MANIFEST_BEFORE="$_MANIFEST_BEFORE" python3 -c "$_MANIFEST_DELTA_PY" 2>/dev/null)" \
        || _degraded="check:manifest-interpreter-failed"
    if [ -n "$_degraded" ]; then
        echo "  NOTE: subsystems not ok after the restart: $_degraded"
    fi
    # Page only for something actionable. A lone `check:no-baseline` means only
    # that there was no pre-restart manifest to compare with (a first deploy, or
    # a restart of a stopped unit) — reported above, not alerted.
    if [ -n "$_degraded" ] && [ "$_degraded" != "check:no-baseline" ]; then
        queue_alert warning deploy-code-only \
            "code-only deploy at $SHA: subsystems not ok" \
            "genesis-server is serving $SHA, but these subsystems regressed or could not be checked across the restart: $_degraded. Check: journalctl --user -u genesis-server -n 100"
    fi
    _read_serving
    if [ "$SERVING" != "$SHA" ]; then
        echo "  NOTE: the reflog does not confirm the restarted server booted from $SHA (${SERVING:-unknown: $SERVING_WHY})."
    fi
    echo "  Healthy — deployed $SHA (genesis-server pid $_SERVER_PID)"
    exit 0
fi
_ALERTED=1
if [ "$_answered_by_other" = true ]; then
    _why="the health endpoint answered, but not from the restarted genesis-server unit (a server running outside systemd may hold the port)"
else
    _why="genesis-server did not pass its health check"
fi
queue_alert critical deploy-code-only \
    "code-only deploy unhealthy at $SHA" \
    "After a code-only deploy to $SHA, $_why. The tree was NOT reverted (by design). Check: journalctl --user -u genesis-server -n 50"
die "after the restart, $_why — tree left at $SHA, alert queued."
