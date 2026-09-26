# shellcheck shell=bash
# deploy_lock.sh — the deploy-station lock (issue #1699).
#
# ONE lock file serializes the runtime + deploy pipeline across every path that
# mutates or measures the live tree:
#
#   * scripts/update.sh          — EXCLUSIVE, non-blocking (its historical
#                                  hard-fail contract is deliberately kept: a
#                                  queued full deploy firing minutes later on a
#                                  box nobody is watching is a surprise deploy;
#                                  update.sh keeps its own inline flock and
#                                  shares only the PATH constant below)
#   * scripts/deploy_code_only.sh — EXCLUSIVE, queues (bounded wait)
#   * validation / E2E runs       — SHARED, queue (scripts/run_under_deploy_lock.sh)
#   * scripts/restore.sh          — EXCLUSIVE, non-blocking (its own copy of
#                                  the same path, like update.sh)
#
# So a contention message must never name only one of these as the holder:
# a validation hold can last hours, and a message saying "another update"
# sends whoever reads it hunting for an update that is not running.
#
# Reader/writer semantics come from flock(2) itself: `-s` holders coexist with
# each other and exclude `-x`, so a deploy waits for in-flight validations and
# a validation waits for an in-flight deploy. There is no lock FILE to go stale:
# the kernel releases the lock when the last copy of the fd closes, so a crashed
# or SIGKILLed holder needs no cleanup — the same property pytest_lock.py
# documents for the test lock.
#
# That is NOT the same as "no stale hold is possible". An fd is inherited by
# children, and the lock survives until EVERY copy closes — so a process that
# outlives its parent keeps the hold. MEASURED 2026-09-06: a backgrounded
# grandchild held the lock after the acquiring script exited, and a new acquirer
# was refused. Two consequences the callers handle rather than the lib:
# run_under_deploy_lock.sh closes the fd in the wrapped command, and the guardian
# renewer's residue is bounded (lib/guardian_pause.sh).
#
# Sourced by deploy_code_only.sh, run_under_deploy_lock.sh and update.sh (the
# lock PATH and the worktree check). Bash 4+.

GENESIS_DEPLOY_LOCK="${GENESIS_DEPLOY_LOCK:-${GENESIS_HOME:-$HOME/.genesis}/locks/update.lock}"

# Exit code for "the wait timed out with the lock still held" — mirrors
# pytest_lock.py's EXIT_LOCK_HELD so operators meet one convention.
DEPLOY_LOCK_HELD_RC=200

# _acquire_deploy_lock <-x|-s> <wait_seconds>
#   Opens the lock fd (kept in _DEPLOY_LOCK_FD; kernel-released when the LAST
#   copy of the fd closes — the acquiring process's own copy is what holds it
#   for the duration, so a caller that runs a child does NOT need the child to
#   inherit it, and should close it in the child: see the leak note above)
#   and blocks up to <wait_seconds> for the requested mode. Returns 0 holding
#   the lock, DEPLOY_LOCK_HELD_RC on timeout, 1 on setup failure.
#   Callers pick the wait: deploys use a short bound (a stuck deploy should
#   surface, not queue forever); validations use the 2h project floor.
_acquire_deploy_lock() {
    local mode="$1" wait_s="$2"
    mkdir -p "$(dirname "$GENESIS_DEPLOY_LOCK")" || return 1
    # Append-mode open: never truncates, and works for shared holders too.
    exec {_DEPLOY_LOCK_FD}>>"$GENESIS_DEPLOY_LOCK" || return 1
    local flock_rc=0
    flock "$mode" -w "$wait_s" "$_DEPLOY_LOCK_FD" || flock_rc=$?
    if [ "$flock_rc" -ne 0 ]; then
        exec {_DEPLOY_LOCK_FD}>&-
        # flock exits 1 when the timeout expires — that, and only that, is
        # contention. Anything else (a usage error, EMFILE, a signal) is a SETUP
        # failure, and reporting it as "the lock is held" sends the operator
        # hunting for a holder that does not exist (Kimi P3, 2026-09-06).
        [ "$flock_rc" -eq 1 ] && return "$DEPLOY_LOCK_HELD_RC"
        return 1
    fi
    return 0
}

acquire_deploy_lock_ex() { _acquire_deploy_lock -x "$1"; }
acquire_deploy_lock_sh() { _acquire_deploy_lock -s "$1"; }

# deploy_marker_pid_live <value>
#   True when <value> names a LIVE process that could own a deploy marker:
#   all digits, greater than 1, and signalable. The digit/floor checks are
#   not decoration — `kill -0 0` and `kill -0 -1` both SUCCEED (they address
#   the process group and every process), so a marker holding 0 would read as
#   a live holder forever while env.update_in_progress() (pid > 1) says no
#   deploy is running. Same predicate env.py and restore.sh apply.
deploy_marker_pid_live() {
    [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -gt 1 ] && kill -0 "$1" 2>/dev/null
}

# deploy_tree_is_linked_worktree <root>
#   True when <root> is a LINKED worktree (git worktree add) rather than the
#   main checkout: a linked worktree's git dir lives under the main repo's
#   .git/worktrees/, so --git-dir differs from --git-common-dir. A
#   path-SUBSTRING test misses worktrees at arbitrary locations
#   (`git worktree add /workspace/feature`), and pip install -e from one
#   redirects the live server's editable installation at it — the exact
#   hazard the callers' worktree refusals exist to prevent (Codex P2, #1804).
#   A non-git <root> answers FALSE: the callers' later rev-parse under set -e
#   already rejects that, and the test seam's fixture trees are plain repos.
deploy_tree_is_linked_worktree() {
    local root="$1" gd gcd
    gd="$(git -C "$root" rev-parse --absolute-git-dir 2>/dev/null)" || return 1
    gcd="$(cd "$root" 2>/dev/null && git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || return 1
    [ -n "$gd" ] && [ -n "$gcd" ] && [ "$gd" != "$gcd" ]
}
