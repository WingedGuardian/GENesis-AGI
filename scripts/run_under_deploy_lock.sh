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
    if [[ "$GENESIS_ROOT" == *"/.claude/worktrees/"* ]] || \
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
fi
cmd_rc=0
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
"$@" {_DEPLOY_LOCK_FD}>&- || cmd_rc=$?
if [ "$RECEIPT" -eq 1 ] && [ "$cmd_rc" -eq 0 ]; then
    append_deploy_receipt "validated" "$SHA" "validation"
fi
exit "$cmd_rc"
