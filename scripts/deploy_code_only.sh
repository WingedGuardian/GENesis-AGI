#!/bin/bash
# deploy_code_only.sh — the everyday code-only deploy, serialized with update.sh.
#
# Runtime code is an editable install (the venv's .pth points at src/), so most
# changes need no reinstall: pull main, restart genesis-server. Done by hand that
# takes no lock, so it lands in the middle of another session's validation or of
# update.sh. This script is that path with the discipline update.sh already has.
#
# Usage: scripts/deploy_code_only.sh [deploy|pull|restart] [--wait N]
#        scripts/deploy_code_only.sh status [--verify <token>]
#
#   deploy   (the default) fetch main and run every check, then stop
#            genesis-server, fast-forward, and start it again (stopped first, so
#            no request runs against a mix of old and new modules).
#   pull     pull main and sync the git hook copies; NO restart. For a change the
#            tree applies by itself (Claude Code hooks, docs), or to stage code
#            for a later restart. Any range is accepted: the report names every
#            change the running server has not loaded, and the next step.
#   restart  restart genesis-server on the tree as it stands; no fetch.
#   status   read-only, takes no lock: the commit the server booted from, HEAD,
#            the server's MainPID and invocation, and what runs beside the commit
#            (runtime-edits, runtime-overrides), and the validation bracket's
#            token; --verify <token> answers whether it still holds (below).
#            Run from a linked worktree, it reports the main checkout, through
#            the main checkout's own copy of this script and its libs.
#   --wait N seconds to queue for the lock (default 7200, the same two hours a
#            validation's hold may run, or GENESIS_DEPLOY_LOCK_WAIT)
#
# Every mode except status runs with:
#   the update.lock, EXCLUSIVE and QUEUING (update.sh keeps `flock -n`, so it
#     REFUSES while a validation holds the lock shared);
#   refusals BEFORE anything changes: a linked worktree, an unfinished update.sh
#     run, a branch other than main, a dirty tree, a unit that runs a different
#     venv or from a different directory, a live foreign deploy marker, and a
#     venv that does not match the pyproject.toml being deployed (that one needs
#     update.sh, which reinstalls). deploy and restart also refuse untracked
#     files under src/, config/ or pyproject.toml, and a server running outside
#     the unit (update.sh's fallback), which a restart would not replace. Just
#     before the restart, four of these are checked again (HEAD must be the
#     exact commit this run checked, on main; no tracked change; no untracked
#     runtime file; no server outside the unit). If one fails while the server
#     is untouched, it is a refusal. If deploy has already stopped the server,
#     it is restarted on the tree as it stands, health-checked, and the run ends
#     in a critical alert: refusing then would leave it down;
#   the deploy marker for the whole run (the watchdog defers; a dashboard update
#     or a restore cannot start underneath).
# pull and deploy add a bounded fetch and a fast-forward-only merge. deploy and
# restart add a Guardian pause (no false "Genesis down" alert or paid diagnosis)
# and a health wait sized the way update.sh sizes its own. deploy skips the stop
# and the restart when the files the server loads (src/, config/,
# pyproject.toml, and the scripts it keeps imported: _RUNTIME_RELOAD_SCRIPTS in
# lib/deploy_status.sh) are the ones it booted from, after the merge and at every
# commit HEAD has held since the boot (the server imports src/ lazily, so a
# pulled tree's module stays loaded after a later commit restores the files).
# The fast-forward never overwrites a file git ignores: git refuses it at the
# merge, as it refuses an untracked one.
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
# $HOME/genesis, and the mode you want for `deploy`. The unit name carries the
# time to the nanosecond, so a second launch queues on the lock instead of
# failing on a name in use. It needs linger
# (`loginctl show-user "$(id -u)" -p Linger` prints Linger=yes), or the unit dies
# at logout:
#   XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" \
#   DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}" \
#   systemd-run --user --collect --unit "genesis-deploy-code-only-$(date +%s%N)" \
#     --working-directory="$HOME/genesis" --setenv=PATH="$PATH" \
#     --setenv=SSH_AUTH_SOCK --setenv=GENESIS_HOME \
#     /bin/bash -c 'mkdir -p ~/tmp; exec ./scripts/deploy_code_only.sh deploy > ~/tmp/deploy-code-only-$(date +%Y%m%d-%H%M%S%N).log 2>&1'
#
# Exit codes: 0 done (or nothing to do) · 200 lock wait timed out · 1 any
# refusal or failure (the message says which; refusals change nothing) · 130 and
# 143 interrupted (SIGINT, SIGTERM), after the exit trap ran. status --verify:
# 0 the token holds, 1 it does not.
#
# Validating against the live server? Hold the lock SHARED for your whole run:
#   flock -s -w 7200 "${GENESIS_HOME:-$HOME/.genesis}/locks/update.lock" <your command>
# and deploy BEFORE you take it (a deploy inside your own hold waits on itself).
# A daemon your command leaves behind keeps holding the lock. While validators'
# holds overlap, a waiting deploy can starve: flock grants a late shared request
# ahead of a queued exclusive one, so the deploy may run out its --wait.
# The hold stops locked deploys, not every restarter (the watchdog, the
# dashboard's service routes, Guardian recovery) and not a bare `git pull`. So
# bracket the run: take the `bracket:` token `status` prints at the start, and
# run `status --verify <token>` at the end. The script decides; exit 0 is a
# valid run. A token exists only when the server is up, its boot commit is known,
# HEAD's runtime files are the ones it booted from and were at every commit HEAD
# held since the boot (after a pull of code, even one a later commit undid:
# restart first), and nothing under src/, config/, pyproject.toml or a script
# the server uses (the two lists in lib/deploy_status.sh) is edited outside
# git; otherwise it prints "unknown (<why>)", which no token matches. It
# covers the boot commit, the MainPID, systemd's invocation id (a pid can be
# reused, an invocation cannot), the scripts the server runs afresh as they
# stand at HEAD (_RUNTIME_FRESH_SCRIPTS: a pull of one voids the token and
# needs no restart) and a fingerprint of the ignored runtime files
# (a config/*.local.yaml, which git status never lists) and of the user overlays
# in ~/.genesis/config, which the loaders prefer. HEAD may move over docs, or
# over hooks and scripts the server never runs, without invalidating the run.
# It is a TRIPWIRE, not a certificate: "valid" means none of those changes
# happened, not that nothing the server runs changed. It cannot see, and reads
# valid through: a change to the venv's installed packages (imported lazily
# too); the other files the server reads from ~/.genesis/config (a user
# outreach.yaml, genesis.yaml, modules/: only the *.local.yaml overlays are
# fingerprinted); a script the server reaches only through a systemd unit it
# starts or a Claude Code session it launches; an edit under src/, config/,
# pyproject.toml or a listed script made and undone
# without moving HEAD (by hand, a stash and its pop, a checkout of a file from
# another commit), including one present at boot; and a reflog rewritten or backdated (`git reflog expire
# --rewrite`, a move made with GIT_COMMITTER_DATE, a clock stepped back). Proving
# what the server runs needs the server to report its own identity.

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
# The loopback address, not a name: nothing in the resolver's configuration can
# point the health request elsewhere.
HEALTH_URL="http://127.0.0.1:5000/api/genesis/health"
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
# shellcheck source=lib/deploy_status.sh
. "$_SELF_DIR/lib/deploy_status.sh"
# The checkout checks and the collision scan, shared with update.sh (needs
# deploy_marker.sh's EPHEMERAL_DIRTY_RE, sourced above).
# shellcheck source=lib/deploy_checkout.sh
. "$_SELF_DIR/lib/deploy_checkout.sh"
# The helpers that run AFTER the merge are read now, like the libs above: the
# merge may replace them on disk, and this run must use the versions it started
# with (`python3 -c "$CODE" args…` sees the same sys.argv as running the file).
# GENESIS_DEPLOY_PORT_PROBE is a TEST seam, like GENESIS_DEPLOY_ROOT: the
# fixture substitutes it so no test ever probes the live server's port.
_PORT_PROBE_PY="$(cat "${GENESIS_DEPLOY_PORT_PROBE:-$_SELF_DIR/lib/port_owned_by.py}")"
_MANIFEST_DELTA_PY="$(cat "$_SELF_DIR/lib/manifest_delta.py")"
_SERVING_COMMIT_PY="$(cat "$_SELF_DIR/lib/serving_commit.py")"

# Kept whole for the status hand-over below (the parse consumes "$@").
_ORIG_ARGS=("$@")
MODE=""
VERIFY=""
# Two hours: a validation's documented hold is `flock -s -w 7200`, and a detached
# deploy that gave up sooner would leave nobody to retry it.
WAIT_S="${GENESIS_DEPLOY_LOCK_WAIT:-7200}"
while [ $# -gt 0 ]; do
    case "$1" in
        deploy|pull|restart|status)
            [ -z "$MODE" ] || die "one mode at a time (got '$MODE' and '$1')."
            MODE="$1"; shift ;;
        --wait) WAIT_S="${2:?--wait needs a value}"; shift 2 ;;
        --verify) VERIFY="${2:?--verify needs the token status printed}"; shift 2 ;;
        --no-pull) die "--no-pull is now the restart mode: scripts/deploy_code_only.sh restart" ;;
        --no-restart) die "--no-restart is now the pull mode: scripts/deploy_code_only.sh pull" ;;
        *) die "unknown argument: $1 (modes: deploy, pull, restart, status)" ;;
    esac
done
MODE="${MODE:-deploy}"
[ -z "$VERIFY" ] || [ "$MODE" = status ] || die "--verify belongs to status (scripts/deploy_code_only.sh status --verify <token>)."
case "$WAIT_S" in
    ''|*[!0-9]*) die "the lock wait must be a whole number of seconds (got: $WAIT_S)" ;;
esac

# ── A linked worktree is never a deploy target ────────────────────────
# Git answers this (genesis_is_primary_checkout, in the shared lib); a path test
# alone misses a worktree added anywhere.
genesis_checkout_git_dirs "$GENESIS_ROOT"
# status is read-only, and validators usually work in a worktree: from one, it
# reports the main checkout, whose .git is the common dir.
if [ "$MODE" = status ] && [ -n "$_git_dir" ] && [ -n "$_common_dir" ] \
    && [ "$(unset CDPATH; cd -- "$_git_dir" && pwd -P)" != "$_common_dir" ] \
    && [ "$(basename -- "$_common_dir")" = .git ]; then
    GENESIS_ROOT="$(dirname -- "$_common_dir")"
    _git_dir="$_common_dir"
    echo "(from a linked worktree: reporting the main checkout, $GENESIS_ROOT)"
    # What `status` hashes and reports (the runtime path lists in lib/deploy_status.sh)
    # belongs to the tree being reported, and this worktree's copy, sourced above, can
    # be older or newer than main's. Hand over to the main checkout's own script with
    # the same arguments. GENESIS_DEPLOY_ROOT is dropped so that script takes its root
    # from where it lives; the guard variable stops a second hand-over. A main
    # checkout with no copy of this script keeps this one (and its lists).
    _main_self="$GENESIS_ROOT/scripts/deploy_code_only.sh"
    if [ -z "${GENESIS_DEPLOY_STATUS_HANDOVER:-}" ] && [ -f "$_main_self" ] \
        && [ "$(readlink -f -- "$_main_self")" != "$(readlink -f -- "${BASH_SOURCE[0]}")" ]; then
        exec env -u GENESIS_DEPLOY_ROOT GENESIS_DEPLOY_STATUS_HANDOVER=1 \
            bash "$_main_self" "${_ORIG_ARGS[@]}"
    fi
fi
genesis_is_primary_checkout "$GENESIS_ROOT" "$_git_dir" "$_common_dir" \
    || die "deploy_code_only.sh must run against the main checkout, not a worktree ($GENESIS_ROOT)."

# ── Checks a restart depends on ───────────────────────────────────────
# A server outside the unit, such as update.sh's nohup fallback after a failed
# restart, is out of systemctl's reach: stopping the unit leaves it serving (and
# lazily importing) from a tree the fast-forward is about to change, and a
# restart would not replace it. The server's process lock names its pid; one
# that is alive, is a `genesis serve`, and is not the unit's MainPID is outside.
# Sets _OUTSIDE_PID.
_server_outside_unit() {
    local lock="$HOME/.genesis/genesis-server.lock" pid main cmd
    [ -f "$lock" ] || return 1
    pid="$(tr -dc '0-9' < "$lock" 2>/dev/null || true)"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    [[ "$cmd" == *"genesis serve"* ]] || return 1
    main="$(systemctl --user show genesis-server -p MainPID --value 2>/dev/null || true)"
    [ "$pid" != "$main" ] || return 1
    _OUTSIDE_PID="$pid"
}

# Untracked files under what the server loads (the editable install imports a
# new module there; startup globs YAML under config/), less the paths update.sh
# excuses. Prints them; nothing when there are none. Unreadable dies.
_untracked_runtime() {
    local st
    st="$(git -C "$GENESIS_ROOT" status --porcelain --no-renames --untracked-files=all \
        -- src config pyproject.toml)" || die "cannot read the working tree's status — nothing changed."
    printf '%s\n' "$st" | grep '^??' | grep -vE "$EPHEMERAL_DIRTY_RE" || true
}


if [ "$MODE" = status ]; then
    _status_main "$VERIFY" && exit 0
    exit 1
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
_RESET_NOTE=""
_cleanup() {
    local rc=$?
    # A second signal during cleanup must not cut it short (skipping the marker
    # release or the Guardian resume).
    trap '' INT TERM
    # A server this run stopped and has not started again goes back up on the
    # tree as it stands, before anything else: never left down.
    local _restarted_note=""
    if [ -n "${_STOPPED:-}" ]; then
        if systemctl --user start genesis-server {_UPDATE_LOCK_FD}>&- 2>/dev/null; then
            _restarted_note=" genesis-server, stopped for this deploy, was started again on that tree without a health check."
        else
            _restarted_note=" genesis-server was stopped for this deploy and could NOT be started again: it is DOWN."
        fi
    fi
    local _resets_note=""
    if [ -n "${_RESET_NOTE:-}" ]; then
        _resets_note=" Reset to HEAD for the merge, their local edits dropped (they regenerate):$_RESET_NOTE."
    fi
    if [ "$rc" -ne 0 ] && [ -z "$_ALERTED" ] && [ "$_PHASE" != "checks" ]; then
        local _sha
        _sha="$(git -C "$GENESIS_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
        queue_alert critical deploy-code-only \
            "code-only deploy failed at phase $_PHASE ($_sha)" \
            "The code-only deploy ($MODE) stopped at phase '$_PHASE'. The tree is at $_sha and was NOT reverted (by design).${_restarted_note}${_resets_note} If the merge ran but no restart did, a running server still has the old code in memory and imports new files lazily. Converge by hand: journalctl --user -u genesis-server -n 50, then scripts/deploy_code_only.sh restart."
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
# No override here: GENESIS_ALLOW_NON_DEPLOY_BRANCH is update.sh's alone.
genesis_deploy_branch_ok "$GENESIS_ROOT" \
    || die "$GENESIS_ROOT is on '${_branch:-a detached HEAD}', not $DEPLOY_BRANCH."
# Unreadable refuses (see genesis_tracked_dirty_paths for why it is read apart).
_dirty="$(genesis_tracked_dirty_paths "$GENESIS_ROOT")" \
    || die "cannot read the working tree's status — nothing was deployed."
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
_unit_dir_is_ours \
    || die "genesis-server runs in ${_UNIT_DIR:-a working directory systemctl did not report}, not $GENESIS_ROOT — run scripts/update.sh --post-merge, whose bootstrap renders the unit for this checkout."
# What this run checked, and what a restart must find: HEAD now, or the upstream
# commit once a fast-forward lands.
_CHECKED="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"
if [ "$MODE" != pull ]; then
    _extra="$(_untracked_runtime)"
    if [ -n "$_extra" ]; then
        echo "ERROR: untracked files under src/, config/ or pyproject.toml would load with the restart. Nothing was deployed:" >&2
        echo "$_extra" >&2
        exit 1
    fi
    if _server_outside_unit; then
        die "a genesis server outside the systemd unit (pid $_OUTSIDE_PID) holds the server lock; stopping or restarting the unit would not replace it — stop it, or run scripts/update.sh, which does."
    fi
fi
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

# The pre-restart identity and manifest, read while the old server is ALIVE (a
# stopped unit reports MainPID 0, and its manifest then belongs to nobody). They
# are what makes the health check about the RESTARTED unit rather than about
# whatever answers on the port, and the baseline for the subsystem delta. Read
# once per run.
_BASELINE_READ=""
_read_baseline() {
    [ -z "$_BASELINE_READ" ] || return 0
    _SERVER_PID_BEFORE="$(systemctl --user show genesis-server -p MainPID --value 2>/dev/null || true)"
    _SERVER_INV_BEFORE="$(systemctl --user show genesis-server -p InvocationID --value 2>/dev/null || true)"
    _MANIFEST_BEFORE="$(cat "$HOME/.genesis/bootstrap_manifest.json" 2>/dev/null || true)"
    _read_serving
    echo "  The server booted from ${SERVING:-an unknown commit ($SERVING_WHY)}."
    _BASELINE_READ=1
}

# The last word before any restart, as close to it as the script can put it: the
# checkout must still be exactly the commit this run checked (_CHECKED), with no
# tracked change and no untracked file under what the server loads, and no
# server may be running outside the unit. The lock serializes deploys, not a
# bare git command or an editor, so this is checked here, not only at the start.
# Returns 1 with _BLOCKED saying why; the caller decides what a "no" means (a
# refusal while the server is untouched, see _restart_or_refuse).
_checkout_unmoved() {
    local head br
    head="$(git -C "$GENESIS_ROOT" rev-parse HEAD 2>/dev/null || echo unreadable)"
    br="$(git -C "$GENESIS_ROOT" symbolic-ref --short -q HEAD || true)"
    [ "$head" = "$_CHECKED" ] && [ "$br" = "$_branch" ] && return 0
    _BLOCKED="the checkout moved during this run (now ${br:-a detached HEAD} at $head; this run checked $_CHECKED) — something outside the lock changed it"
    return 1
}
_ready_to_restart() {
    local st dirty extra
    _checkout_unmoved || return 1
    if ! st="$(git -C "$GENESIS_ROOT" status --porcelain --no-renames --untracked-files=all 2>/dev/null)"; then
        _BLOCKED="cannot read the working tree's status before the restart"
        return 1
    fi
    dirty="$(printf '%s\n' "$st" | grep -v '^??' | grep -vE "$EPHEMERAL_DIRTY_RE" | grep -v '^$' || true)"
    if [ -n "$dirty" ]; then
        _BLOCKED="tracked files changed during this run: $(printf '%s\n' "$dirty" | head -n 5 | cut -c4- | paste -sd ' ' -)"
        return 1
    fi
    extra="$(printf '%s\n' "$st" | grep '^?? ' | cut -c4- | grep -E '^(src/|config/|pyproject\.toml$)' \
        | sed 's/^/?? /' | grep -vE "$EPHEMERAL_DIRTY_RE" | cut -c4- || true)"
    if [ -n "$extra" ]; then
        _BLOCKED="untracked files under src/, config/ or pyproject.toml would load: $(printf '%s\n' "$extra" | head -n 5 | paste -sd ' ' -) — commit or move them aside"
        return 1
    fi
    if _server_outside_unit; then
        _BLOCKED="a server outside the systemd unit (pid $_OUTSIDE_PID) holds the server lock, and a restart would not replace it — stop it, or run scripts/update.sh, which does"
        return 1
    fi
}

# After a "no" from the checks above: while the server is untouched, a refusal
# (nothing changed, nobody paged). Once deploy has stopped it, refusing would
# leave it down, so the restart goes ahead on the tree as it stands, with the
# health and identity checks, and the run ends in a critical alert naming why
# the tree was not the one checked (owner ruling on #2557, 2026-09-29).
_UNVERIFIED=""
_restart_or_refuse() {
    [ -n "$_STOPPED" ] || die "$_BLOCKED — nothing was restarted."
    [ -z "$_UNVERIFIED" ] || return 0
    _UNVERIFIED="$_BLOCKED"
    SHA="$(git -C "$GENESIS_ROOT" rev-parse HEAD 2>/dev/null || echo unreadable)"
    echo "  WARNING: $_UNVERIFIED." >&2
    echo "  The server is stopped, so it is restarted on the tree as it stands ($SHA), health-checked, and alerted." >&2
}

# deploy's stop before the fast-forward. _STOPPED stays set until the server is
# started again; the exit trap starts it if this run ends first, so a failure
# never leaves it down.
_STOPPED=""
_PAUSED_THIS_RUN=""
_stop_for_deploy() {
    _read_baseline
    _guardian_pause
    _PAUSED_THIS_RUN=1
    _PHASE="stopping"
    echo "  Stopping genesis-server before the fast-forward…"
    _STOPPED=1
    systemctl --user stop genesis-server {_UPDATE_LOCK_FD}>&-
    _PHASE="stopped"
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
    # Paths the range brings in that already exist here, untracked (on a fast-forward
    # these can only be additions: every path it changes is tracked here) (or with a file
    # where a parent directory goes): git would overwrite an IGNORED one without
    # asking. The scan is the shared lib's genesis_range_collisions; only what git
    # does NOT track counts.
    _coll_rc=0
    _collisions="$(genesis_range_collisions "$GENESIS_ROOT" "$_head" "$_upstream")" || _coll_rc=$?
    [ "$_coll_rc" -ne 2 ] || die "cannot list the files this range adds — nothing changed."
    if [ -n "$_collisions" ]; then
        _collisions+=$'\n'
        echo "ERROR: this range adds files that already exist here, untracked; the fast-forward would overwrite them:" >&2
        printf '%s' "$_collisions" | sed 's/^/         /' >&2
        die "move them aside first (scripts/update.sh carries .claude/settings.local.json, .serena/project.yml and src/genesis/identity/USER.md across) — nothing changed."
    fi
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
    # deploy stops the server BEFORE the fast-forward, as update.sh does: the
    # editable install imports src/ from disk, so a live server would serve
    # requests against a mix of old and new modules until its restart. pull keeps
    # the server running by design, and reports the pending changes instead.
    # A range that leaves the server's files as it booted them (docs, hooks)
    # needs neither the stop nor the restart, but only if HEAD has held no other
    # runtime files since the boot: a module the server imported from a pulled
    # tree stays loaded after a later commit restores the files.
    if [ "$MODE" = deploy ]; then
        _read_baseline
        if [ -n "$SERVING" ] && _runtime_held "$_upstream"; then
            echo "  Nothing the server loads changes in $_head..$_upstream, nor since its boot: no stop, no restart."
        else
            _stop_for_deploy
        fi
    fi
    # An excused file the range also changes is reset to HEAD so the merge can
    # take upstream's copy. Both regenerate (the code-intel indexer rewrites
    # AGENTS.md, the server the trigger cache), so this script keeps no copy: each
    # reset is NAMED instead, here, in a refusal's message and in the exit alert,
    # so none passes silently. (update.sh differs: it backs such edits up under
    # ~/.genesis/premerge-backups before its merge.)
    _reset=()
    for _f in AGENTS.md config/procedure_triggers.yaml; do
        if git -C "$GENESIS_ROOT" ls-files --error-unmatch "$_f" >/dev/null 2>&1 \
            && ! git -C "$GENESIS_ROOT" diff --quiet HEAD -- "$_f" 2>/dev/null \
            && [ -n "$(_range_changed "$_f")" ]; then
            _reset+=("$_f")
        fi
    done
    # The tree's state before this run touches it, leaving out the files reset
    # below: a merge git refuses counts as "nothing merged" only when HEAD and
    # this status come back unchanged.
    _status_outside_resets() {
        local p excl=()
        for p in "${_reset[@]}"; do excl+=(":(exclude,literal)$p"); done
        git -C "$GENESIS_ROOT" status --porcelain -- . "${excl[@]}" 2>/dev/null
    }
    _status_before="$(_status_outside_resets)" || _status_before="unreadable before"
    _PHASE="merging"
    for _f in "${_reset[@]}"; do
        echo "  Resetting $_f to HEAD for the merge: its local edit is dropped (it regenerates)."
        git -C "$GENESIS_ROOT" checkout HEAD -- "$_f"
        _RESET_NOTE="$_RESET_NOTE $_f"
    done
    # Safe for this script to merge the tree it runs from: git REPLACES a changed
    # file rather than rewriting it in place, so bash keeps reading the old copy
    # (measured, git 2.43). update.sh copies itself to temp instead.
    # A merge git refuses (an untracked file in the way, say) leaves the tree
    # where it was: that is a refusal like the others, not a failure to alert on.
    # "Where it was" means HEAD AND the working tree's status outside the resets,
    # so a merge that failed partway still alerts.
    # --no-overwrite-ignore: git overwrites an IGNORED file in the way by default.
    # The collision scan above refuses early, while the server is untouched, but
    # a file written between that scan and this merge (the server, another
    # session) would be lost; with the flag git refuses it too, at the merge
    # itself, leaving HEAD and the tree as they were (measured, git 2.43).
    if ! git -c gc.autoDetach=false -C "$GENESIS_ROOT" merge --ff-only --no-overwrite-ignore -q "$_upstream" {_UPDATE_LOCK_FD}>&-; then
        _status_after="$(_status_outside_resets)" || _status_after="unreadable after"
        if [ "$(git -C "$GENESIS_ROOT" rev-parse HEAD 2>/dev/null)" = "$_head" ] \
            && [ "$_status_after" = "$_status_before" ]; then
            # Nothing merged, and a server this run stopped goes back up on the
            # same tree; only if that fails does the exit still alert.
            if [ -n "$_STOPPED" ]; then
                echo "  Starting genesis-server again on the unchanged tree…"
                systemctl --user start genesis-server {_UPDATE_LOCK_FD}>&- && _STOPPED=""
            fi
            [ -n "$_STOPPED" ] || _PHASE="checks"
            die "git refused the fast-forward to $_upstream (see above) — nothing merged.${_RESET_NOTE:+ Reset to HEAD for it, their local edits dropped (they regenerate):$_RESET_NOTE.}"
        fi
        exit 1
    fi
    _PHASE="merged"
    _CHECKED="$_upstream"
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
        # deploy syncs after its restart: the server may be stopped now, and the
        # sync runs a script from the merged tree that has no reason to lengthen
        # the outage.
        [ "$MODE" = deploy ] || _sync_git_hooks
        ;;
    restart)
        # No fetch, so nothing to merge: update.sh --post-merge is what reinstalls.
        _deps_ok "the working tree" "scripts/update.sh --post-merge" < "$GENESIS_ROOT/pyproject.toml" || exit 1
        ;;
esac
# The lock serializes deploys, not a bare git command: if the checkout moved
# after this run checked it (during the merge or a pull's hook sync), what it
# holds is not what was checked. The same check runs again just before a restart.
SHA="$_CHECKED"
_checkout_unmoved || _restart_or_refuse

if [ "$MODE" = pull ]; then
    _report_pending "$_PULL_FROM"
    echo "  Pulled — tree at $SHA (no restart)."
    exit 0
fi

# ── Restart ───────────────────────────────────────────────────────────
# (deploy with something to merge read the baseline before the fast-forward;
# every other path reads it here, server still alive.)
_read_baseline
# A deploy with nothing to deploy: the server is running, its boot commit is
# known, and the files it loads are the same at HEAD and at every commit HEAD
# held since the boot (a merge of docs, or of hooks and scripts the server does
# not keep imported, or no merge at all). A
# restart would only cost an outage and end in-flight dispatched sessions. The
# restart mode is there to force one.
if [ "$MODE" = deploy ] && [ -z "$_STOPPED" ] && [ -n "$SERVING" ] && _runtime_held "$SHA"; then
    echo "  Nothing to deploy: the files the server loads are the ones it booted from ($SERVING), and have been since."
    _sync_git_hooks
    echo "  Tree at $SHA; no restart."
    exit 0
fi
# The reflog and the unit's start time both count whole seconds, and the boot
# commit is only readable when no move of HEAD shares the boot's second. A second
# of wait puts this boot clear of the merge above.
sleep 1
[ -n "$_PAUSED_THIS_RUN" ] || _guardian_pause
_ready_to_restart || _restart_or_refuse
_PHASE="restarting"
echo "  Restarting genesis-server at $SHA…"
# A restart stops the old process before it starts the new one, and the start
# can fail after the stop: armed, the exit trap starts the server again, so a
# failed restart never leaves it down unannounced.
_STOPPED=1
systemctl --user restart genesis-server {_UPDATE_LOCK_FD}>&-
_STOPPED=""
_PHASE="restarted"
[ "$MODE" = deploy ] && _sync_git_hooks

# Is the RESTARTED unit the one serving? A 200 from the port alone cannot say: a
# server started outside systemd (update.sh's nohup fallback) can keep answering
# while the new unit exits on the process lock it holds. And the bootstrap
# manifest cannot say either: it is written BEFORE the web server binds, and Flask
# runs in a daemon thread, so a failed bind leaves the process up with its
# manifest while something else answers. So the proof is the socket itself: the
# unit is active in a NEW activation with a nonzero MainPID, and every socket
# listening on the health port is one of that pid's own descriptors
# (scripts/lib/port_owned_by.py, which reads /proc and answers no whenever it
# cannot tell; its code was read at startup). The activation, not the pid, is
# what shows the restart happened: the kernel can give the new process the old
# pid, but systemd never reuses an invocation id. Only when the old invocation
# was unreadable does a changed pid stand in for it. Prints the pid.
_HEALTH_PORT=5000
_restarted_unit_serving() {
    local state pid inv
    state="$(systemctl --user is-active genesis-server 2>/dev/null || true)"
    [ "$state" = active ] || return 1
    pid="$(systemctl --user show genesis-server -p MainPID --value 2>/dev/null || true)"
    [ -n "$pid" ] && [ "$pid" != 0 ] || return 1
    if [ -n "${_SERVER_INV_BEFORE:-}" ]; then
        inv="$(systemctl --user show genesis-server -p InvocationID --value 2>/dev/null || true)"
        [ -n "$inv" ] && [ "$inv" != "$_SERVER_INV_BEFORE" ] || return 1
    else
        [ "$pid" != "$_SERVER_PID_BEFORE" ] || return 1
    fi
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
    # The answer and the identity must be about ONE process: read the unit's pid
    # before the request and require the same pid after it (a restart in between
    # would otherwise let one process's answer vouch for its replacement). -q
    # (first, as curl requires) skips any .curlrc, and --noproxy '*' any proxy
    # the environment sets, so the request goes straight to the local port.
    _pid_asked="$(systemctl --user show genesis-server -p MainPID --value 2>/dev/null || true)"
    if curl -q --noproxy '*' -sf --max-time 20 "$HEALTH_URL" >/dev/null 2>&1; then
        if _SERVER_PID="$(_restarted_unit_serving)" && [ "$_SERVER_PID" = "$_pid_asked" ]; then
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
    if [ -n "$_UNVERIFIED" ]; then
        _ALERTED=1
        queue_alert critical deploy-code-only \
            "code-only deploy restarted on a tree it did not check ($SHA)" \
            "The code-only deploy had stopped genesis-server when a late check found: $_UNVERIFIED. Refusing would have left the server down, so it was restarted on the tree as it stands ($SHA) and is healthy, but that tree is not the commit this run checked ($_CHECKED). Converge: find what changed the checkout, then scripts/deploy_code_only.sh restart."
        die "restarted on a tree this run did not check ($SHA): $_UNVERIFIED — alert queued."
    fi
    exit 0
fi
_ALERTED=1
[ -z "$_UNVERIFIED" ] || _UNVERIFIED=" It was also not the tree this run checked: $_UNVERIFIED."
if [ "$_answered_by_other" = true ]; then
    _why="the health endpoint answered, but not from the restarted genesis-server unit (a server running outside systemd may hold the port)"
else
    _why="genesis-server did not pass its health check"
fi
queue_alert critical deploy-code-only \
    "code-only deploy unhealthy at $SHA" \
    "After a code-only deploy to $SHA, $_why.$_UNVERIFIED The tree was NOT reverted (by design). Check: journalctl --user -u genesis-server -n 50"
die "after the restart, $_why — tree left at $SHA, alert queued."
