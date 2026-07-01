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

echo "=== genesis-disk-hygiene done ==="
