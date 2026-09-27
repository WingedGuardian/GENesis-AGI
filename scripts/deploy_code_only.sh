#!/bin/bash
# deploy_code_only.sh — the everyday code-only deploy, serialized with update.sh.
#
# Runtime code is an editable install (the venv's .pth points at src/), so most
# changes need no reinstall: pull main, restart genesis-server. Done by hand that
# takes no lock, so it lands in the middle of another session's validation or of
# update.sh. This script is that path with the discipline update.sh already has:
#
#   the update.lock, EXCLUSIVE and QUEUING (update.sh keeps `flock -n`, so it
#     REFUSES while a validation holds the lock shared);
#   refusals BEFORE anything changes: a linked worktree, an unfinished update.sh
#     run, a branch other than main, a dirty tree, a live foreign deploy marker,
#     and a pull whose pyproject.toml the venv does not satisfy (that one needs
#     update.sh, which reinstalls);
#   a bounded fetch and a fast-forward-only merge;
#   the deploy marker (the watchdog defers) and a Guardian pause (no false
#     "Genesis down" alert or paid diagnosis) across the restart;
#   a health wait sized the way update.sh sizes its own.
#
# ON FAILURE: ALERT AND HOLD, no auto-revert (owner decision, 2026-09-06). The
# tree IS the install, so moving it backwards under every live session (hooks,
# scripts and agent definitions resolve from it) is a bigger hazard than a bad
# deploy. A critical alert is queued and the tree is left where it is.
#
# Pending DB migrations apply when the server boots, with no pre-migration
# snapshot — the same as any bare restart (owner ruling, 2026-09-26).
#
# Usage: scripts/deploy_code_only.sh [--wait N] [--no-pull] [--no-restart]
#   --wait N      seconds to queue for the lock (default 900, or
#                 GENESIS_DEPLOY_LOCK_WAIT)
#   --no-pull     restart the tree as it stands (still dependency-checked)
#   --no-restart  a locked pull only: for a hooks or docs change, which takes
#                 effect without a restart. Refuses a range that changes src/.
# It is long-running (up to the lock wait plus the health window): run it in the
# background from an agent session, never under a short tool timeout.
#
# Exit codes: 0 deployed (or nothing to do) · 200 lock wait timed out · 1 any
# refusal or failure (the message says which; refusals change nothing).
#
# Validating against the live server? Hold the lock SHARED for your whole run:
#   flock -s -w 7200 ~/.genesis/locks/update.lock <your command>
# and deploy BEFORE you take it (a deploy inside your own hold waits on itself).
# The hold stops locked deploys, not every restarter (the watchdog, the
# dashboard's service routes, Guardian recovery). So also record, at the start and
# at the end, `systemctl --user show -p MainPID,ActiveEnterTimestamp genesis-server`
# and `git rev-parse HEAD`: if either differs, the run is invalid.

set -euo pipefail

if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "ERROR: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

# GENESIS_DEPLOY_ROOT is a TEST seam: tests point it at a fixture tree so no test
# ever restarts the real runtime. Unset, it is the checkout this script lives in.
GENESIS_ROOT="${GENESIS_DEPLOY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
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
_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/guardian_pause.sh
. "$_SELF_DIR/lib/guardian_pause.sh"
# shellcheck source=lib/deploy_marker.sh
. "$_SELF_DIR/lib/deploy_marker.sh"
# shellcheck source=lib/alert_queue.sh
. "$_SELF_DIR/lib/alert_queue.sh"

WAIT_S="${GENESIS_DEPLOY_LOCK_WAIT:-900}"
DO_PULL=1
DO_RESTART=1
while [ $# -gt 0 ]; do
    case "$1" in
        --wait) WAIT_S="${2:?--wait needs a value}"; shift 2 ;;
        --no-pull) DO_PULL=0; shift ;;
        --no-restart) DO_RESTART=0; shift ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 1 ;;
    esac
done
case "$WAIT_S" in
    ''|*[!0-9]*) echo "ERROR: the lock wait must be a whole number of seconds (got: $WAIT_S)" >&2; exit 1 ;;
esac

die() { echo "ERROR: $*" >&2; exit 1; }
# A positive whole number by VALUE: `00` and `000` are zero, whatever they look like.
_positive_int() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$((10#$1))" -gt 0 ]; }

# ── A linked worktree is never a deploy target ────────────────────────
# Git answers this; a path test alone misses a worktree added anywhere. The path
# arms stay for a plain clone parked under a worktree directory.
_git_dir="$(git -C "$GENESIS_ROOT" rev-parse --absolute-git-dir 2>/dev/null || true)"
_common_dir="$(cd "$GENESIS_ROOT" 2>/dev/null && cd "$(git rev-parse --git-common-dir 2>/dev/null || echo /nonexistent)" 2>/dev/null && pwd -P || true)"
if [ -z "$_git_dir" ] || [ -z "$_common_dir" ] || [ "$(cd "$_git_dir" && pwd -P)" != "$_common_dir" ] \
    || [[ "$GENESIS_ROOT" == *"/.claude/worktrees/"* ]] || [[ "$GENESIS_ROOT" == *"/.worktrees/"* ]]; then
    die "deploy_code_only.sh must run against the main checkout, not a worktree ($GENESIS_ROOT)."
fi

echo ""
echo "  Genesis code-only deploy"
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
            "The code-only deploy stopped at phase '$_PHASE'. The tree is at $_sha and was NOT reverted (by design). If the merge ran but the restart did not, the running server still has the old code in memory and imports new files lazily. Converge by hand: journalctl --user -u genesis-server -n 50, then restart or re-run scripts/deploy_code_only.sh."
    fi
    _guardian_resume
    _release_deploy_marker
}
trap _cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ── Refusals: nothing has changed yet ─────────────────────────────────
[ ! -e "$UPDATE_STATE_FILE" ] \
    || die "$UPDATE_STATE_FILE records an unfinished update.sh run; finish it with scripts/update.sh --post-merge."
_branch="$(git -C "$GENESIS_ROOT" symbolic-ref --short -q HEAD || true)"
[ "$_branch" = main ] || die "$GENESIS_ROOT is on '${_branch:-a detached HEAD}', not main."
_dirty="$(git -C "$GENESIS_ROOT" status --porcelain 2>/dev/null | grep -v '^??' | grep -vE "$EPHEMERAL_DIRTY_RE" || true)"
if [ -n "$_dirty" ]; then
    echo "ERROR: $GENESIS_ROOT has uncommitted tracked changes. Nothing was deployed:" >&2
    echo "$_dirty" >&2
    exit 1
fi
# The deploy marker is taken NOW, before any pull, in every mode: a live foreign
# holder (a dashboard update between its tiers, a restore) is refused before the
# tree moves, and nothing can take the marker between our check and our restart.
# Released by _cleanup.
if ! _acquire_deploy_marker; then
    die "a live deploy (pid $DEPLOY_MARKER_HOLDER) holds $DEPLOY_MARKER_FILE — a dashboard update or a restore is running."
fi

# Does the venv satisfy a pyproject.toml read from stdin? A code-only deploy never
# reinstalls, so an unsatisfied dependency must go through update.sh.
_deps_ok() {
    local out rc=0
    out="$("$VENV_DIR/bin/python" "$_SELF_DIR/lib/venv_satisfies_pyproject.py" 2>&1)" || rc=$?
    [ "$rc" -eq 0 ] && return 0
    echo "ERROR: the venv does not satisfy $1's dependencies — run scripts/update.sh instead:" >&2
    echo "$out" | sed 's/^/         /' >&2
    return 1
}

# ── Pull ──────────────────────────────────────────────────────────────
if [ "$DO_PULL" -eq 1 ]; then
    _fetch_timeout="${GENESIS_DEPLOY_FETCH_TIMEOUT:-120}"
    # `timeout 0` means NO limit, and so does `timeout 00`: judge the VALUE, never
    # the spelling.
    _positive_int "$_fetch_timeout" \
        || die "GENESIS_DEPLOY_FETCH_TIMEOUT must be a positive number of seconds (got: $_fetch_timeout)"
    _fetch_timeout=$((10#$_fetch_timeout))
    echo "  Fetching (bounded, ${_fetch_timeout}s)…"
    # Every git that can outlive this script (an auto-gc) runs in the foreground
    # and without the lock fd.
    timeout -k 10 "$_fetch_timeout" git -c gc.autoDetach=false -C "$GENESIS_ROOT" fetch {_UPDATE_LOCK_FD}>&- \
        || die "fetch failed or timed out — nothing changed."
    _upstream="$(git -C "$GENESIS_ROOT" rev-parse --verify -q '@{u}' || true)"
    [ -n "$_upstream" ] || die "main has no upstream to pull from."
    _head="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"
    if [ "$_head" != "$_upstream" ]; then
        git -C "$GENESIS_ROOT" merge-base --is-ancestor "$_head" "$_upstream" \
            || die "main has diverged from its upstream — nothing changed; reconcile by hand."
        # The dependency gate reads the INCOMING pyproject.toml, before the merge,
        # so a refusal leaves the tree untouched.
        git -C "$GENESIS_ROOT" show "$_upstream:pyproject.toml" | _deps_ok "$_upstream" \
            || exit 1
        # --no-restart promises a hooks/docs deploy. Advancing runtime code under a
        # running server without restarting it leaves old code in memory importing
        # new files lazily, so that combination is refused.
        if [ "$DO_RESTART" -eq 0 ] && [ -n "$(git -C "$GENESIS_ROOT" diff --name-only "$_head" "$_upstream" -- src/)" ]; then
            die "this range changes runtime code under src/ — deploy it without --no-restart."
        fi
        _range_changed() { git -C "$GENESIS_ROOT" diff --name-only "$_head" "$_upstream" -- "$@"; }
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
        # Advisory: what this deploy does NOT activate. The path lists are read from
        # the repo's own deploy-health snapshot (AST, no import), never re-listed.
        mapfile -t _tier2 < <("$VENV_DIR/bin/python" - "$GENESIS_ROOT" TIER2_PATHS <<'PY' 2>/dev/null || true
import ast, pathlib, sys
src = pathlib.Path(sys.argv[1], "src/genesis/observability/snapshots/deploy_health.py").read_text()
for node in ast.parse(src).body:
    if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == sys.argv[2] for t in node.targets):
        print("\n".join(ast.literal_eval(node.value)))
PY
)
        mapfile -t _guardian < <("$VENV_DIR/bin/python" - "$GENESIS_ROOT" GUARDIAN_HOST_PATHS <<'PY' 2>/dev/null || true
import ast, pathlib, sys
src = pathlib.Path(sys.argv[1], "src/genesis/observability/snapshots/deploy_health.py").read_text()
for node in ast.parse(src).body:
    if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == sys.argv[2] for t in node.targets):
        print("\n".join(ast.literal_eval(node.value)))
PY
)
        if [ "${#_tier2[@]}" -gt 0 ]; then
            _activation="$(_range_changed "${_tier2[@]}" || true)"
            if [ -n "$_activation" ]; then
                echo "  NOTE: this range changes activation paths a code-only deploy does not apply"
                echo "        (systemd units, the bootstrap, git hook install, dependencies). Run scripts/update.sh for those:"
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
        git -c gc.autoDetach=false -C "$GENESIS_ROOT" merge --ff-only -q "$_upstream" {_UPDATE_LOCK_FD}>&-
        _PHASE="merged"
    else
        echo "  Already at the upstream tip."
        _deps_ok "the working tree" < "$GENESIS_ROOT/pyproject.toml" || exit 1
    fi
else
    _deps_ok "the working tree" < "$GENESIS_ROOT/pyproject.toml" || exit 1
fi
SHA="$(git -C "$GENESIS_ROOT" rev-parse HEAD)"

if [ "$DO_RESTART" -eq 0 ]; then
    echo "  Tree at $SHA (no restart requested)."
    exit 0
fi

# ── Restart ───────────────────────────────────────────────────────────
_guardian_pause
_PHASE="restarting"
echo "  Restarting genesis-server at $SHA…"
systemctl --user restart genesis-server {_UPDATE_LOCK_FD}>&-
_PHASE="restarted"

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
_attempt=0
while [ "$_attempt" -lt "$_max_attempts" ] && [ "$(_now)" -lt "$_deadline" ]; do
    _attempt=$((_attempt + 1))
    sleep "$_poll"
    if curl -sf --max-time 20 "$HEALTH_URL" >/dev/null 2>&1; then
        _healthy=true
        break
    fi
    # An empty state (systemd/D-Bus busy) is unreadable, not dead: keep waiting.
    _state="$(systemctl --user is-active genesis-server 2>/dev/null || true)"
    case "$_state" in
        active|activating|reloading|'') echo "  Attempt $_attempt: not answering yet (unit: ${_state:-unreadable})…" ;;
        *) echo "  Attempt $_attempt: unit is '$_state' — it will not come up on its own."; break ;;
    esac
done

if [ "$_healthy" = true ]; then
    echo "  Healthy — deployed $SHA"
    exit 0
fi
_ALERTED=1
queue_alert critical deploy-code-only \
    "code-only deploy unhealthy at $SHA" \
    "genesis-server did not pass its health check after a code-only deploy to $SHA. The tree was NOT reverted (by design). Check: journalctl --user -u genesis-server -n 50"
die "genesis-server is not healthy after the restart — tree left at $SHA, alert queued."
