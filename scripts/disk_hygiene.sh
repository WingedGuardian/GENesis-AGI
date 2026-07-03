#!/usr/bin/env bash
# genesis-disk-hygiene — daily disk grooming entrypoint.
#
# Run by the genesis-disk-hygiene.timer systemd unit (also runnable by hand).
# Two steps, both best-effort (one failing must not skip the other):
#   1. Reap merged/inactive git worktrees  → scripts/worktree_lifecycle.py
#      (trash-bin with 7-day recovery; frees space when trash purges)
#   2. Reclaim regenerable caches          → scripts/disk_reclaim.py
#      (cheap tier always; medium/reindex tier only when disk >= 90%)
#
# Note: run under a hardened systemd sandbox (NoNewPrivileges, ProtectSystem=
# strict), so disk_reclaim's --system (/var, sudo) path is intentionally NOT
# passed here — it would no-op anyway. /var reclaim is the reactive path's job.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

VENV_PY="$REPO_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    VENV_PY="$(command -v python3 || true)"
fi
if [ -z "$VENV_PY" ]; then
    echo "disk_hygiene: no python interpreter found" >&2
    exit 1
fi

echo "=== genesis-disk-hygiene $(date -u +%FT%TZ) ==="

echo "--- worktree reaping ---"
"$VENV_PY" "$REPO_DIR/scripts/worktree_lifecycle.py" || echo "worktree_lifecycle exited $?"

echo "--- cache reclamation ---"
"$VENV_PY" "$REPO_DIR/scripts/disk_reclaim.py" --apply --if-above 90 || echo "disk_reclaim exited $?"

# Reap orphaned per-session background-CC sandboxes (~/tmp/bg-cc-sessions/<id>).
# direct_session._run_session removes these in a finally on normal completion;
# this catches orphans left when a session is hard-SIGKILLed (skips finally).
# 24h is well past any live session: the Genesis-controlled max timeout is
# 7200s/2h (CCInvocation.timeout_s); DirectSessionRequest defaults to 3600s/1h.
echo "--- background CC sandbox reaping ---"
BG_CC_SANDBOX_DIR="$HOME/tmp/bg-cc-sessions"
if [ -d "$BG_CC_SANDBOX_DIR" ]; then
    find "$BG_CC_SANDBOX_DIR" -mindepth 1 -maxdepth 1 -type d -mmin +1440 \
        -exec rm -rf {} + 2>/dev/null || echo "bg-cc-sandbox reap exited $?"
fi

echo "=== genesis-disk-hygiene done ==="
