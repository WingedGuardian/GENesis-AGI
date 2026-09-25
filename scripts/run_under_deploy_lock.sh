#!/bin/bash
# run_under_deploy_lock.sh — hold the deploy-station lock around a command
# (issue #1699's reader side).
#
# A validation/E2E run against the live server takes the lock SHARED: shared
# holders coexist with each other, while a deploy (exclusive) waits for them
# and they wait for a deploy — so a restart can never land mid-measurement.
#
# Usage: scripts/run_under_deploy_lock.sh [--shared|--exclusive] [--wait N]
#                                         -- <command> [args…]
#   --shared     (default) coexist with other validations; exclude deploys
#   --exclusive  a writer hold, for a caller that mutates the runtime itself
#   --wait N     seconds to queue (default 7200 — the project's 2h floor: a
#                validation queued behind a long deploy should run, not flake)
#
# Exit: the wrapped command's code; 200 (DEPLOY_LOCK_HELD_RC) on lock timeout.

set -euo pipefail

# Resolve HOME when unset, before sourcing the lib: deploy_lock.sh derives its
# lock path from ${HOME}, so under `set -u` a stripped-env invocation would
# abort inside the source rather than here. The scanner only flags scripts that
# dereference $HOME directly, which this one does not — the exposure arrives
# through the lib, so the guard belongs here anyway.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "ERROR: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

# shellcheck source=lib/deploy_lock.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/deploy_lock.sh"

MODE="sh"
WAIT_S=7200
while [ $# -gt 0 ]; do
    case "$1" in
        --shared) MODE="sh"; shift ;;
        --exclusive) MODE="ex"; shift ;;
        --wait) WAIT_S="${2:?--wait needs a value}"; shift 2 ;;
        --) shift; break ;;
        *) echo "ERROR: unknown argument before '--': $1" >&2; exit 1 ;;
    esac
done
if [ $# -eq 0 ]; then
    echo "ERROR: no command given (usage: run_under_deploy_lock.sh [opts] -- cmd…)" >&2
    exit 1
fi
case "$WAIT_S" in
    ''|*[!0-9]*) echo "ERROR: --wait must be a positive integer (got: $WAIT_S)" >&2; exit 1 ;;
esac

rc=0
"acquire_deploy_lock_$MODE" "$WAIT_S" || rc=$?
if [ "$rc" -ne 0 ]; then
    # Same split deploy_code_only.sh makes: only a timeout means a holder exists.
    if [ "$rc" -eq "$DEPLOY_LOCK_HELD_RC" ]; then
        echo "ERROR: deploy lock not acquired within ${WAIT_S}s ($GENESIS_DEPLOY_LOCK, mode $MODE)." >&2
    else
        echo "ERROR: could not open the deploy lock ($GENESIS_DEPLOY_LOCK, mode $MODE)." >&2
    fi
    exit "$rc"
fi

# Run the command WITHOUT the lock fd. The hold does not depend on inheritance —
# this process keeps its own copy open for the whole wait — but inheritance is a
# LEAK CHANNEL: a wrapped command that leaves a background process behind (an E2E
# suite orphaning a helper is the ordinary case) hands that orphan a duplicate of
# the fd, and flock is released only when the LAST copy closes. The orphan then
# holds the lock after this script exits, so every later deploy queues its full
# --wait and fails with a message naming no holder, until someone finds the stray
# pid by hand. MEASURED 2026-09-06: an inherited copy does keep the lock after the
# acquirer exits, and closing it in the child releases it as expected.
# update.sh already carries this exact guard for its nohup fallback, pinned by
# test_nohup_fallback_closes_lock_fd — this is that known class, not a new theory.
#
# The fd-close has a mirror-image hazard, and both are handled: with the fd closed
# in the child, a wrapped command that OUTLIVES this wrapper (only the wrapper was
# signalled) keeps running with NO lock held — a deploy could then restart the
# live server mid-validation. So the command runs as a tracked child, and a TERM
# or INT to the wrapper is FORWARDED to the child before the lock is released
# (Codex P2, #1804). SIGKILL can never be forwarded; grandchildren that survive
# their parent are the accepted residue — they hold no lock fd, which is the
# property the station depends on.
cmd_rc=0
_cmd_pid=""
_cmd_done=""
_wrapper_on_signal() {
    # Landing after the child was already reaped: there is nothing left to
    # forward to, and the command's own result is the honest exit code.
    [ -n "$_cmd_done" ] && exit "$cmd_rc"
    local sig="${1:-TERM}" rc=143
    [ "$sig" = "INT" ] && rc=130
    trap - TERM INT HUP
    # Bash runs a trap BETWEEN commands, so a signal can land after `"$@" &`
    # forked the child but before `_cmd_pid=$!` recorded it. `$!` already names
    # the child then; this script starts no other background job before the
    # launch, so an empty `$!` means the child was never started.
    [ -n "$_cmd_pid" ] || _cmd_pid="${!:-}"
    if [ -n "$_cmd_pid" ]; then
        kill "$_cmd_pid" 2>/dev/null || true
        # Bounded grace, then SIGKILL: a TERM-ignoring child must not wedge the
        # station — before forwarding existed the wrapper died and released the
        # lock immediately (adversarial audit, #1804). The watchdog subshell
        # runs WITHOUT the lock fd, so even it cannot extend the hold.
        ( sleep 10; kill -9 "$_cmd_pid" 2>/dev/null || true ) {_DEPLOY_LOCK_FD}>&- &
        local _watchdog=$!
        wait "$_cmd_pid" 2>/dev/null || true
        kill "$_watchdog" 2>/dev/null || true
        wait "$_watchdog" 2>/dev/null || true
    fi
    exit "$rc"
}
# Traps armed BEFORE the child launch: a TERM landing between spawn and trap
# install would take the default disposition — the wrapper dies, the kernel
# releases the lock, and the child keeps validating while a deploy restarts the
# server under it (Devin #1804). With the trap first, that same signal runs
# _wrapper_on_signal: _cmd_pid is empty only if the child was never started, in
# which case the handler exits having forwarded nothing.
# NOTE: a wrapped command backgrounded from a non-job-control shell inherits
# SIGINT as SIG_IGN (POSIX), so INT forwarding is inert for callers that spawn
# us from scripts — the TERM path is the live one.
# HUP too: a closed terminal or a supervisor's HUP to the wrapper alone would
# otherwise take the default disposition and release the lock under a child
# that keeps running.
trap '_wrapper_on_signal TERM' TERM HUP
trap '_wrapper_on_signal INT' INT
"$@" {_DEPLOY_LOCK_FD}>&- &
_cmd_pid=$!
wait "$_cmd_pid" || cmd_rc=$?
_cmd_done=1
trap - TERM INT HUP
exit "$cmd_rc"
