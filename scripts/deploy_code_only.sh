#!/bin/bash
# deploy_code_only.sh — the code-only deploy path, made a station (issue #1699).
#
# The common validation loop — pull main, reinstall, restart genesis-server —
# used to exist only as prose ("git pull && pip install -e . && systemctl
# --user restart genesis-server"), which meant: no lock (two sessions could
# interleave restarts), no deploy marker (the autonomy watchdog stayed armed and
# could revive the server mid-restart), no guardian pause (the host guardian
# could read the restart window as an incident and walk its remediation
# ladder), and no health verification. This wrapper is that path with the
# discipline update.sh already has, scaled to a restart-sized window:
#
#   exclusive deploy lock (QUEUES; shared with update.sh's lock file and with
#   validation runs' shared holds — scripts/lib/deploy_lock.sh)
#   → update_in_progress.pid (env.update_in_progress() → watchdog stands down;
#     liveness-checked, so a crashed run self-heals)
#   → guardian gateway pause, short TTL, bounded renewer (lib/guardian_pause.sh)
#   → bounded fetch + ff-only merge (skip with --no-pull) → pip install -e .
#   → restart → health verify (endpoint + unit).
#
# ON A FAILED HEALTH CHECK: ALERT AND HOLD — no auto-revert (owner decision,
# 2026-09-06). pip install -e means the TREE is the install, so reverting moves
# the main checkout backwards under every live session (hooks, $ROOT scripts
# and agent definitions resolve from it per-invocation) — a bigger hazard than
# the bad deploy. We queue a critical alert, resume the guardian (whose
# remediation ladder is the designed responder), and exit nonzero with the tree
# untouched. Automated rollback can be earned later per the staged-authority
# ladder (spec §8.10b).
#
# Usage: scripts/deploy_code_only.sh [--wait N] [--no-pull]
#   --wait N    seconds to queue for the deploy lock (default 600 — a deploy
#               blocked longer than a validation hold's typical length should
#               surface to the operator, not fire unattended later)
#   --no-pull   restart + reinstall the tree as it stands (no fetch/merge)
#
# Exit codes: 0 deployed+healthy · 200 lock wait timed out (DEPLOY_LOCK_HELD_RC)
#             · any other NONZERO code on failure: 1 for this script's own
#             refusals and the health failure, and a failing step's own code
#             (git's 128, pip's, systemctl's) passes through `set -e` as is.

set -euo pipefail

# Resolve HOME when unset: a stripped-env/systemd/sandbox invocation leaves HOME
# unset, which under `set -u` aborts at the first ${HOME} use — here the marker
# path, before any deploy work. Same passwd fallback update.sh carries, for the
# same reason; fail closed if unresolvable. (Guarded by
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
# The watchdog stand-down marker. This path writes the bare-PID signal
# env.update_in_progress() honours (the one restore.sh and the dashboard update
# already use), NOT update.sh's update_state.json. That file is update.sh's
# crash-recovery record: bootstrap reads a dead run's entry as a crashed FULL
# update and resets the tree to recover it, so a SIGKILLed code-only run that had
# written there made bootstrap discard the tree's tracked edits (Devin, #1804).
# A dead PID in this file is read as "no deploy" by every reader, so a killed run
# leaves nothing anything acts on. GENESIS_HOME-aware, like env.genesis_home().
PID_FILE="${GENESIS_HOME:-$HOME/.genesis}/update_in_progress.pid"
# update.sh's crash-recovery record (the path update.sh writes). Read only: its
# presence means a full update has not finished, and deploying over that would
# strand the recovery `update.sh --post-merge` performs.
UPDATE_STATE_FILE="$HOME/.genesis/update_state.json"
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
# helper lives there), BEFORE any marker write or lock acquisition.
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

# Deploy progress, for the failure alert: a run that advanced the tree
# (merged/installed/restarted) and then failed must say so loudly, because the
# still-running server gives no sign of it.
_PHASE="init"
_ALERTED=""

_release_pid_marker() {
    # Remove the marker ONLY if it is still ours — restore.sh's owner check.
    # Another writer's live marker is not ours to delete.
    if [ -f "$PID_FILE" ] && [ "$(cat "$PID_FILE" 2>/dev/null || true)" = "$$" ]; then
        rm -f "$PID_FILE" 2>/dev/null || true
    fi
}

_cleanup() {
    local rc=$?
    if [ "$rc" -ne 0 ] && [ -z "$_ALERTED" ] && [ "$_PHASE" != "init" ]; then
        local _fail_sha
        _fail_sha="$(git -C "$GENESIS_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
        # Alert-and-hold (Codex P1, #1804): past the tree-advance point a failed
        # deploy is silent otherwise. A failed install after a merge leaves the
        # STILL-RUNNING server lazily importing a mixture of old in-memory
        # modules and newly merged files, indefinitely, because the process stays
        # "healthy"; a failed install can also leave the venv WITHOUT the package
        # (pip -e removes the old dist first), breaking the next lazy import; a
        # failed restart can leave the server down. Same doctrine as the
        # health-failure path: no auto-revert, a human converges process and tree.
        queue_alert critical deploy-code-only \
            "code-only deploy failed at $_PHASE ($_fail_sha)" \
            "code-only deploy failed at phase '$_PHASE'. If the merge advanced the tree, the running genesis-server was NOT restarted and may lazily import a mix of old and new code; a failed pip install -e may have removed the old editable dist, so the next lazy import breaks; a failed restart may have left the server down. Tree left at $_fail_sha — no auto-revert by design. Converge by hand: journalctl --user -u genesis-server -n 50, then restart or re-run scripts/deploy_code_only.sh."
    fi
    _guardian_resume
    _release_pid_marker
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

# An unfinished full update (architect SF1): update.sh's crash/conflict path
# leaves its state file holding the rollback identity that `update.sh
# --post-merge` reads back to finish the recovery. Its owner is dead by then, so
# the flock is free; deploying over it would build on a half-recovered tree.
if [ -f "$UPDATE_STATE_FILE" ]; then
    echo "ERROR: $UPDATE_STATE_FILE records an unfinished update.sh run." >&2
    echo "       Finish it first: scripts/update.sh --post-merge" >&2
    echo "       (or remove the file deliberately if you know it is stale)." >&2
    exit 1
fi
# A LIVE foreign holder of the marker (a dashboard update or a restore holding
# the server stopped) is refused, exactly as restore.sh refuses us. A dead PID
# is a stale marker every reader already ignores, so it is simply replaced.
if [ -f "$PID_FILE" ]; then
    _other="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ "$_other" != "$$" ] && deploy_marker_pid_live "$_other"; then
        echo "ERROR: $PID_FILE is held by a live process (pid $_other) — a dashboard update" >&2
        echo "       or a restore is running. Retry when it ends." >&2
        exit 1
    fi
fi

# Marker + traps arm only once the lock is ours: a contention exit above must
# leave no marker behind. INT/TERM route through EXIT so cleanup always runs
# (130/143 per signal convention).
trap _cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$(dirname "$PID_FILE")"
echo "$$" > "$PID_FILE"
_guardian_pause

if [ "$DO_PULL" -eq 1 ]; then
    # A pull advances whatever branch is CHECKED OUT, but every artifact around
    # it — this log line, the SKILL contract, and the operator's belief — says
    # "main". If the serving checkout was left on another branch (a manual
    # rollback, an interrupted intervention), a pull would deploy THAT branch
    # while reporting main. Refuse instead of guessing; --no-pull remains the
    # deliberate escape for a deploy of an already-checked-out tree (Kimi P3,
    # 2026-09-06).
    _BRANCH="$(git -C "$GENESIS_ROOT" symbolic-ref --short -q HEAD || echo "")"
    if [ "$_BRANCH" != "main" ]; then
        echo "ERROR: refusing to pull — $GENESIS_ROOT is on '${_BRANCH:-a detached HEAD}', not main." >&2
        echo "       A pull here would advance that branch while reporting main." >&2
        echo "       Check main out, or re-run with --no-pull to deploy the tree as it stands." >&2
        exit 1
    fi
    # The network half is BOUNDED, and split from the merge: a remote that
    # accepts the connection then goes silent would otherwise hold the
    # exclusive station lock indefinitely (Codex P2, #1804). timeout 120 is the
    # bound update.sh gives its own fetch; bounding the merge instead could kill
    # it mid-checkout. `git fetch` with no arguments fetches main's configured
    # upstream, and `@{u}` names it — exactly what `git pull --ff-only` does.
    # The env knob exists for the test suite only; the default IS the policy.
    _FETCH_TIMEOUT="${GENESIS_DEPLOY_FETCH_TIMEOUT:-120}"
    # `timeout 0` means NO limit, so a zero or non-numeric knob would silently
    # remove the very bound this block exists for.
    case "$_FETCH_TIMEOUT" in
        ''|*[!0-9]*|0) echo "ERROR: GENESIS_DEPLOY_FETCH_TIMEOUT must be a positive integer (got: $_FETCH_TIMEOUT)" >&2; exit 1 ;;
    esac
    echo "  Fetching main (bounded, ${_FETCH_TIMEOUT}s)…"
    # -k 10: a fetch that ignores TERM is killed outright rather than
    # outliving the bound.
    if ! timeout -k 10 "$_FETCH_TIMEOUT" git -C "$GENESIS_ROOT" fetch; then
        echo "ERROR: fetch failed or timed out — nothing changed, server untouched." >&2
        exit 1
    fi
    git -C "$GENESIS_ROOT" merge --ff-only '@{u}'
    _PHASE="pulled"
fi
SHA="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"
echo "  Deploying $SHA"

echo "  pip install -e (venv)…"
# Phase marker BEFORE the install (adversarial audit, #1804): pip -e removes the
# old dist before installing, so an install failure leaves the venv WITHOUT the
# package — the running server then breaks on its next lazy import, silently.
# With --no-pull the phase would otherwise still be "init" and _cleanup would
# skip the alert.
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
    echo "  Healthy — deployed $SHA"
    exit 0
fi

# ALERT AND HOLD (see header): the tree stays where it is, a human decides.
_ALERTED=1
queue_alert critical deploy-code-only \
    "code-only deploy unhealthy at $SHA" \
    "genesis-server failed health verification after a code-only deploy (pip install -e + restart). Tree left at $SHA — no auto-revert by design. Check: journalctl --user -u genesis-server -n 50"
echo "ERROR: genesis-server unhealthy after restart — tree left at $SHA, alert queued." >&2
exit 1
