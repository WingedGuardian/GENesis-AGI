#!/bin/bash
# deploy_code_only.sh — the code-only deploy path, made a station (issue #1699).
#
# The common validation loop — pull main, reinstall, restart genesis-server —
# used to exist only as prose ("git pull && pip install -e . && systemctl
# --user restart genesis-server"), which meant: no lock (two sessions could
# interleave restarts), no deploy state (the autonomy watchdog stayed armed and
# could revive the server mid-restart), no guardian pause (the host guardian
# could read the restart window as an incident and walk its remediation
# ladder), no health verification, and no record tying "deployed" to a SHA.
# This wrapper is that path with the discipline update.sh already has, scaled
# to a restart-sized window:
#
#   exclusive deploy lock (QUEUES; shared with update.sh's lock file and with
#   validation runs' shared holds — scripts/lib/deploy_lock.sh)
#   → update_state.json (env.update_in_progress() → watchdog stands down;
#     PID-liveness + 4h staleness make it self-healing if we crash)
#   → guardian gateway pause, short TTL, bounded renewer (lib/guardian_pause.sh)
#   → git pull --ff-only (skip with --no-pull) → pip install -e . → restart
#   → health verify (endpoint + unit) → deployed-SHA receipt.
#
# ON A FAILED HEALTH CHECK: ALERT AND HOLD — no auto-revert (owner decision,
# 2026-09-06). pip install -e means the TREE is the install, so reverting moves
# the main checkout backwards under every live session (hooks, $ROOT scripts
# and agent definitions resolve from it per-invocation) — a bigger hazard than
# the bad deploy. We queue a critical alert, write a health_failed receipt,
# resume the guardian (whose remediation ladder is the designed responder),
# and exit nonzero with the tree untouched. Automated rollback can be earned
# later per the staged-authority ladder (spec §8.10b).
#
# Usage: scripts/deploy_code_only.sh [--wait N] [--no-pull]
#   --wait N    seconds to queue for the deploy lock (default 600 — a deploy
#               blocked longer than a validation hold's typical length should
#               surface to the operator, not fire unattended later)
#   --no-pull   restart + reinstall the tree as it stands (no git pull)
#
# Exit codes: 0 deployed+healthy · 200 lock wait timed out (DEPLOY_LOCK_HELD_RC)
#             · 1 anything else (message says what; health_failed included).

set -euo pipefail

# Resolve HOME when unset: a stripped-env/systemd/sandbox invocation leaves HOME
# unset, which under `set -u` aborts at the first ${HOME} use — here STATE_FILE,
# before any deploy work. Same passwd fallback update.sh carries, for the same
# reason; fail closed if unresolvable. (Guarded by
# tests/test_scripts/test_home_guard_coverage.py.)
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "ERROR: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

# GENESIS_DEPLOY_ROOT: test seam (install-agnostic tests point it at a fixture
# tree so no test ever reinstalls or restarts the real runtime). Unset = the
# checkout this script lives in, which is the only production form.
GENESIS_ROOT="${GENESIS_DEPLOY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV_DIR="$GENESIS_ROOT/.venv"
STATE_FILE="$HOME/.genesis/update_state.json"
# NOTE: an earlier revision also wrote ~/.genesis/deploy_window.json. It was
# removed rather than wired: nothing anywhere read it, and every field it carried
# (active/started_at/pid) is already in STATE_FILE — which additionally carries
# `phase` and `path` and IS read, by env.update_in_progress(). Two markers
# holding the same fact, one of them unread, is the replica-drift shape this
# repo keeps paying for; and on SIGKILL the unread one leaked `{"active": true}`
# with nothing to reconcile it, so a future reader could have taken a dead run
# for a live deploy window (Kimi P3, 2026-09-06).
HEALTH_URL="http://localhost:5000/api/genesis/health"

# CC sessions lack D-Bus env vars, making `systemctl --user` fail — the same
# guard update.sh carries, for the same primary caller.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

# Libs load from THIS script's directory (not $GENESIS_ROOT): under the test
# seam the target tree is a fixture with no scripts/lib.
_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/deploy_lock.sh
source "$_SELF_DIR/lib/deploy_lock.sh"

# Same refusal update.sh makes, same reason: pip install -e from a worktree
# redirects system-wide imports at the live server (measured incident). A
# linked worktree can live at ANY path (`git worktree add /workspace/x`), so a
# path-substring test alone would wave it through — ask Git instead (Codex P2,
# #1804). The substring arms stay as well: a plain CLONE sitting under a marker
# dir is not a linked worktree and fools the git check, but carries the same
# pip -e hazard (adversarial audit, #1804). Sits AFTER the lib source (the
# helper lives there), BEFORE any state write or lock acquisition.
if deploy_tree_is_linked_worktree "$GENESIS_ROOT" || \
   [[ "$GENESIS_ROOT" == *"/.claude/worktrees/"* ]] || \
   [[ "$GENESIS_ROOT" == *"/.worktrees/"* ]]; then
    echo "ERROR: deploy_code_only.sh must not run from a worktree." >&2
    echo "       GENESIS_ROOT=$GENESIS_ROOT — run the main checkout's copy." >&2
    exit 1
fi
if [ -n "${GENESIS_DEPLOY_ROOT:-}" ]; then
    echo "  NOTE: GENESIS_DEPLOY_ROOT override in effect — deploying $GENESIS_ROOT"
fi
# Short pause window: this path's server-DOWN span is one restart (seconds),
# not update.sh's stop→merge→bootstrap span. Renewer bound unchanged (~10min
# worst-case silence if we are SIGKILLed; host expires_at is the hard TTL).
# shellcheck disable=SC2034  # consumed by lib/guardian_pause.sh's `:=` default,
# which is sourced on the NEXT line — shellcheck cannot follow that and reports
# it unused. Silenced explicitly so a real unused-variable warning here is not
# lost in known noise.
GUARDIAN_PAUSE_TTL=300
# shellcheck source=lib/guardian_pause.sh
source "$_SELF_DIR/lib/guardian_pause.sh"
# shellcheck source=lib/alert_queue.sh
source "$_SELF_DIR/lib/alert_queue.sh"

WAIT_S=600
DO_PULL=1
while [ $# -gt 0 ]; do
    case "$1" in
        --wait) WAIT_S="${2:?--wait needs a value}"; shift 2 ;;
        --no-pull) DO_PULL=0; shift ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 1 ;;
    esac
done
case "$WAIT_S" in
    ''|*[!0-9]*) echo "ERROR: --wait must be a positive integer (got: $WAIT_S)" >&2; exit 1 ;;
esac

_write_state() {
    # Minimal update_state.json in the shape env.update_in_progress() reads:
    # counts while phase != "done", started_at recent, and $$ alive — so a
    # crashed wrapper self-heals with no TTL bookkeeping of ours.
    mkdir -p "$HOME/.genesis"
    cat > "$STATE_FILE" << SEOF
{
    "phase": "$1",
    "started_at": "$(date -Iseconds)",
    "pid": $$,
    "path": "code-only"
}
SEOF
}

# Deploy progress marker for the failure receipt below: a run that advanced
# the tree (pulled/installed/restarted) but never completed its health verify
# must leave a ledger row, or the next validation hold would record
# "validated" at a HEAD the server never loaded.
_PHASE="init"
_RECEIPTED=""

_cleanup() {
    local rc=$?
    if [ "$rc" -ne 0 ] && [ -z "$_RECEIPTED" ] && [ "$_PHASE" != "init" ]; then
        local _fail_sha
        _fail_sha="$(git -C "$GENESIS_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
        append_deploy_receipt "deploy_failed" \
            "$_fail_sha" \
            "code-only" "failed at $_PHASE"
        # Alert-and-hold (Codex P1, #1804): past the tree-advance point a failed
        # deploy is silent otherwise — the receipt is a ledger row nobody reads
        # live. A failed install after a pull leaves the STILL-RUNNING server
        # lazily importing a mixture of old in-memory modules and newly pulled
        # files, indefinitely, because the process stays "healthy"; a failed
        # install can also leave the venv WITHOUT the package (pip -e removes
        # the old dist first), breaking the next lazy import; a failed restart
        # can leave the server down. Same doctrine as the health_failed path:
        # no auto-revert, a human converges process and tree.
        queue_alert critical deploy-code-only \
            "code-only deploy failed at $_PHASE ($_fail_sha)" \
            "code-only deploy failed at phase '$_PHASE'. If the pull advanced the tree, the running genesis-server was NOT restarted and may lazily import a mix of old and new code; a failed pip install -e may have removed the old editable dist, so the next lazy import breaks; a failed restart may have left the server down. Tree left at $_fail_sha — no auto-revert by design. Converge by hand: journalctl --user -u genesis-server -n 50, then restart or re-run scripts/deploy_code_only.sh. Receipts: $GENESIS_DEPLOY_RECEIPTS"
    fi
    _guardian_resume
    rm -f "$STATE_FILE" 2>/dev/null || true
}

echo ""
echo "  Genesis code-only deploy"
echo "  ────────────────────────"

rc=0
acquire_deploy_lock_ex "$WAIT_S" || rc=$?
if [ "$rc" -ne 0 ]; then
    if [ "$rc" -eq "$DEPLOY_LOCK_HELD_RC" ]; then
        echo "ERROR: deploy lock still held after ${WAIT_S}s ($GENESIS_DEPLOY_LOCK)." >&2
        echo "       Another deploy or a validation hold is running — retry when it ends." >&2
    else
        echo "ERROR: could not open the deploy lock ($GENESIS_DEPLOY_LOCK)." >&2
    fi
    exit "$rc"
fi
echo "  Deploy lock held (exclusive)"

# REFUSE-DON'T-CLOBBER (architect SF1): update.sh's crash/conflict path leaves
# update_state.json holding the rollback identity (`rollback_tag`/`old_commit`)
# that `update.sh --post-merge` reads back to finish the recovery. Its owner
# PID is dead by then, so the flock is free — overwriting and deleting that
# file here would strand the recovery. Our OWN dead leftover (a SIGKILLed
# code-only run, marked `"path": "code-only"`) carries no recovery state and
# is safe to replace.
if [ -f "$STATE_FILE" ] && ! grep -q '"path": "code-only"' "$STATE_FILE" 2>/dev/null; then
    echo "ERROR: $STATE_FILE holds an unfinished update.sh run's recovery state." >&2
    echo "       Finish it first: scripts/update.sh --post-merge" >&2
    echo "       (or remove the file deliberately if you know it is stale)." >&2
    exit 1
fi

# State + traps arm only once the lock is ours: a contention exit above must
# leave no marker behind. INT/TERM route through EXIT so cleanup always runs
# (130/143 per signal convention).
trap _cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
_write_state "code-only"
_guardian_pause

if [ "$DO_PULL" -eq 1 ]; then
    # `git pull --ff-only` advances whatever branch is CHECKED OUT, but every
    # artifact around it — this log line, the receipt, the SKILL contract, and
    # the operator's belief — says "main". If the serving checkout was left on
    # another branch (a manual rollback, an interrupted intervention), the old
    # code deployed and receipted THAT branch while reporting main: a false
    # entry in the ledger built to make deploy claims trustworthy. Refuse
    # instead of guessing; --no-pull remains the deliberate escape for a
    # deploy of an already-checked-out tree (Kimi P3, 2026-09-06).
    _BRANCH="$(git -C "$GENESIS_ROOT" symbolic-ref --short -q HEAD || echo "")"
    if [ "$_BRANCH" != "main" ]; then
        echo "ERROR: refusing to pull — $GENESIS_ROOT is on '${_BRANCH:-a detached HEAD}', not main." >&2
        echo "       A pull here would advance and receipt that branch while reporting main." >&2
        echo "       Check main out, or re-run with --no-pull to deploy the tree as it stands." >&2
        exit 1
    fi
    echo "  Pulling main (ff-only)…"
    git -C "$GENESIS_ROOT" pull --ff-only
    _PHASE="pulled"
fi
SHA="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"
echo "  Deploying $SHA"

echo "  pip install -e (venv)…"
# Phase marker BEFORE the install (adversarial audit, #1804): pip -e removes the
# old dist before installing, so an install failure leaves the venv WITHOUT the
# package — the running server then breaks on its next lazy import, silently.
# With --no-pull the phase would otherwise still be "init" and _cleanup would
# skip both the failure receipt and the alert.
_PHASE="installing"
"$VENV_DIR/bin/pip" install -e "$GENESIS_ROOT" --quiet
_PHASE="installed"

echo "  Restarting genesis-server…"
systemctl --user restart genesis-server
_PHASE="restarted"

# Health verify: the SAME 12 × 15s envelope update.sh gives this phase — the
# thing being waited out is the server's own boot, which applies pending DB
# migrations at startup, and that cost is identical on both paths. A shorter
# budget here would fire a false critical alert on any migration-carrying
# boot (architect SF3). The env knobs exist for the test suite only; the
# defaults ARE the policy.
HEALTH_OK=false
for _ in $(seq 1 "${GENESIS_DEPLOY_HEALTH_ATTEMPTS:-12}"); do
    if curl -sf --max-time 20 "$HEALTH_URL" >/dev/null 2>&1; then
        HEALTH_OK=true
        break
    fi
    sleep "${GENESIS_DEPLOY_HEALTH_INTERVAL:-15}"
done
if [ "$HEALTH_OK" = true ] && systemctl --user is-active --quiet genesis-server; then
    # Receipt attribution (Codex P2, #1804): the exclusive hold only
    # coordinates cooperating lock users — verify the tree is STILL the SHA we
    # deployed before recording that it is. A HEAD that moved mid-deploy gets a
    # failure receipt + alert, never a false "deployed".
    _NOW_SHA="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"
    if [ "$_NOW_SHA" != "$SHA" ]; then
        append_deploy_receipt "deploy_failed" "$SHA" "code-only" \
            "HEAD moved to $_NOW_SHA during the deploy"
        _RECEIPTED=1
        queue_alert critical deploy-code-only \
            "HEAD moved mid code-only deploy ($SHA -> $_NOW_SHA)" \
            "The serving checkout moved while the deploy held the exclusive station lock — the lock coordinates cooperating users only. The server was restarted onto an unattributed tree; receipts name both SHAs. Investigate who moved $GENESIS_ROOT."
        echo "ERROR: HEAD moved mid-deploy ($SHA -> $_NOW_SHA) — receipt refused, alert queued." >&2
        exit 1
    fi
    # A dirty tree deploys and serves its tracked modifications (pip install -e
    # means the TREE is the install), so a bare "deployed $SHA" would misname
    # what is serving. Deploy proceeds — with --no-pull that is the operator's
    # deliberate act — but the receipt must say so (Codex P2 / Devin, #1804).
    # deploy_tree_serving_diff also counts untracked files under code-bearing
    # paths: an untracked module under src/ is importable by the serving
    # install, not scratch (Codex P2, #1804). The probe fails CLOSED: a git
    # error aborts the receipt via set -e (the EXIT trap then records
    # deploy_failed), never a bare "deployed".
    _DIRTY="$(deploy_tree_serving_diff "$GENESIS_ROOT")"
    if [ -n "$_DIRTY" ]; then
        append_deploy_receipt "deployed" "$SHA" "code-only" \
            "tracked-dirty tree — $SHA does not fully describe the served code"
        echo "  WARNING: tracked uncommitted changes present — receipt for $SHA carries a dirty-tree note." >&2
    else
        append_deploy_receipt "deployed" "$SHA" "code-only"
    fi
    echo "  Healthy — deployed $SHA (receipt: $GENESIS_DEPLOY_RECEIPTS)"
    exit 0
fi

# ALERT AND HOLD (see header): the tree stays where it is, a human decides.
append_deploy_receipt "health_failed" "$SHA" "code-only" "endpoint/unit unhealthy after restart"
_RECEIPTED=1
queue_alert critical deploy-code-only \
    "code-only deploy unhealthy at $SHA" \
    "genesis-server failed health verification after a code-only deploy (pip install -e + restart). Tree left at $SHA — no auto-revert by design. Check: journalctl --user -u genesis-server -n 50; receipts: $GENESIS_DEPLOY_RECEIPTS"
echo "ERROR: genesis-server unhealthy after restart — tree left at $SHA, alert queued." >&2
exit 1
