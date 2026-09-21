#!/bin/bash
# run_under_deploy_lock.sh — hold the deploy-station lock around a command
# (issue #1699's reader side).
#
# A validation/E2E run against the live server takes the lock SHARED: shared
# holders coexist with each other, while a deploy (exclusive) waits for them
# and they wait for a deploy — so a restart can never land mid-measurement and
# a measurement can never be attributed to a tree that was swapped under it.
#
# Usage: scripts/run_under_deploy_lock.sh [--shared|--exclusive] [--wait N]
#                                         [--receipt] -- <command> [args…]
#   --shared     (default) coexist with other validations; exclude deploys
#   --exclusive  a writer hold, for a caller that mutates the runtime itself
#   --wait N     seconds to queue (default 7200 — the project's 2h floor: a
#                validation queued behind a long deploy should run, not flake)
#   --receipt    on command success, append a {status: "validated", sha} line
#                to the deploy receipts — the SHA is read from the main
#                checkout UNDER the lock, so it IS the serving SHA for the
#                whole run (the exclusive/shared exclusion is what makes that
#                claim true rather than a race)
#
# Exit: the wrapped command's code; 200 (DEPLOY_LOCK_HELD_RC) on lock timeout.

set -euo pipefail

# Resolve HOME when unset, before sourcing the lib: deploy_lock.sh derives both
# its lock and receipts paths from ${HOME}, so under `set -u` a stripped-env
# invocation would abort inside the source rather than here. The scanner only
# flags scripts that dereference $HOME directly, which this one does not — the
# exposure arrives through the lib, so the guard belongs here anyway.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "ERROR: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

# GENESIS_DEPLOY_ROOT: test seam, same contract as deploy_code_only.sh.
GENESIS_ROOT="${GENESIS_DEPLOY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# shellcheck source=lib/deploy_lock.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/deploy_lock.sh"

MODE="sh"
WAIT_S=7200
RECEIPT=0
while [ $# -gt 0 ]; do
    case "$1" in
        --shared) MODE="sh"; shift ;;
        --exclusive) MODE="ex"; shift ;;
        --wait) WAIT_S="${2:?--wait needs a value}"; shift 2 ;;
        --receipt) RECEIPT=1; shift ;;
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

# A --receipt run's SHA claim is about the SERVING tree — the main checkout.
# Sessions live in worktrees, and invoking a worktree's copy would record the
# worktree branch's HEAD as "validated" while the server serves main (architect
# SF4). Running FROM a worktree is fine; sourcing the SHA from one is not.
if [ "$RECEIPT" -eq 1 ]; then
    # A `validated` receipt claims "this SHA was the serving tree for the whole
    # run". The SHA is read BEFORE the command, and an EXCLUSIVE hold is precisely
    # the writer mode — it permits a command that moves the checkout, after which
    # the receipt would name the OLD SHA. That is not a weaker claim, it is a false
    # one, in the ledger built to make the claim trustworthy (CodeRabbit Major,
    # 2026-09-06). A validation holds SHARED; there is no honest exclusive receipt.
    if [ "$MODE" != "sh" ]; then
        echo "ERROR: --receipt requires a SHARED hold (--shared, the default)." >&2
        echo "       An exclusive hold may move the checkout under the run, so the" >&2
        echo "       recorded SHA would not be the one that was served." >&2
        exit 1
    fi
    # A linked worktree can live at ANY path (git worktree add /workspace/x), so
    # a path-substring test alone would wave it through; ask Git instead (Codex
    # P2, #1804 — deploy_tree_is_linked_worktree in lib/deploy_lock.sh). The
    # substring arms stay too: a plain CLONE under a marker dir is not a linked
    # worktree, but a receipt naming its HEAD is the same false claim.
    if deploy_tree_is_linked_worktree "$GENESIS_ROOT" || \
       [[ "$GENESIS_ROOT" == *"/.claude/worktrees/"* ]] || \
       [[ "$GENESIS_ROOT" == *"/.worktrees/"* ]]; then
        echo "ERROR: --receipt refused from a worktree copy — the recorded SHA must be" >&2
        echo "       the serving tree's. Invoke the main checkout's copy of this script." >&2
        exit 1
    fi
fi

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

SHA=""
if [ "$RECEIPT" -eq 1 ]; then
    # Read the SHA only when a receipt will use it. Computed unconditionally, a
    # `set -e` abort here would kill a plain `-- make test` run over a value that
    # would never have been read (Kimi P3, 2026-09-06).
    SHA="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"
    # The receipt is a claim about the WHOLE run, so the tree must be clean at
    # acquire time, not just at receipt time: a command that validates dirty
    # modifications and then restores them (`pytest; git checkout -- .`) would
    # otherwise record `validated` for a HEAD the tested code never matched
    # (Codex P2, #1804). Same probe, same fail-closed contract as below.
    _pre_dirty="$(deploy_tree_serving_diff "$GENESIS_ROOT")" || {
        echo "ERROR: could not verify tree cleanliness (git status failed) — refusing the run." >&2
        exit 1
    }
    if [ -n "$_pre_dirty" ]; then
        echo "ERROR: the tree already carries uncommitted code — refusing the receipt run:" >&2
        echo "       a validated receipt for $SHA would describe a tree that was never" >&2
        echo "       served. Commit, stash, or restore first." >&2
        exit 1
    fi
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
# update.sh:784 already carries this exact guard for its nohup fallback, pinned by
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
_wrapper_on_signal() {
    local sig="${1:-TERM}" rc=143
    [ "$sig" = "INT" ] && rc=130
    trap - TERM INT
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
trap '_wrapper_on_signal TERM' TERM
trap '_wrapper_on_signal INT' INT
"$@" {_DEPLOY_LOCK_FD}>&- &
_cmd_pid=$!
wait "$_cmd_pid" || cmd_rc=$?
trap - TERM INT
if [ "$RECEIPT" -eq 1 ] && [ "$cmd_rc" -eq 0 ]; then
    # The receipt claims "this SHA was the serving tree for the whole run". The
    # SHARED hold only coordinates cooperating lock users — the wrapped command
    # itself can still move or dirty the checkout, after which the pre-read SHA
    # is a false attribution in the ledger built to make that claim trustworthy
    # (Codex P2, #1804). Verify the tree's identity AND cleanliness at receipt
    # time; refuse rather than record a claim we cannot stand behind. Both
    # probes fail CLOSED: a git error refuses the receipt, never waves one
    # through. deploy_tree_serving_diff counts tracked changes anywhere plus
    # untracked files under code-bearing paths — an untracked module under
    # src/ IS importable code (Codex P2, #1804); scratch outside those paths
    # stays excluded.
    if [ "$(git -C "$GENESIS_ROOT" rev-parse HEAD)" != "$SHA" ]; then
        echo "ERROR: HEAD moved under the run (was $SHA) — refusing the receipt:" >&2
        echo "       the command validated a different tree than the one it started on." >&2
        exit 1
    fi
    _dirty="$(deploy_tree_serving_diff "$GENESIS_ROOT")" || {
        echo "ERROR: could not verify tree cleanliness (git status failed) — refusing the receipt." >&2
        exit 1
    }
    if [ -n "$_dirty" ]; then
        echo "ERROR: the tree has uncommitted code — refusing the receipt:" >&2
        echo "       the editable install serves those modifications, so $SHA does not" >&2
        echo "       describe what was validated. Commit, stash, or restore first." >&2
        exit 1
    fi
    # A `validated` row claims the SERVER ran $SHA — a clean tree at HEAD only
    # proves the checkout, not the process: a pull that advanced HEAD then
    # failed before restart leaves old code serving while the ledger would
    # still record `validated` for the new SHA (Devin #1804). Require the
    # ledger's newest deploy outcome for $SHA to be `deployed`. The command's
    # own result is unaffected — it ran and passed; only the receipt claim is
    # withheld, loudly, on stderr.
    if deploy_receipt_serving_ok "$SHA"; then
        append_deploy_receipt "validated" "$SHA" "validation"
    else
        echo "WARNING: no validated receipt written for $SHA — the deploy ledger has" >&2
        echo "         no successful 'deployed' row for this SHA, so the serving process" >&2
        echo "         may be running older code. Complete a successful deploy first." >&2
    fi
fi
exit "$cmd_rc"
