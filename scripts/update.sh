#!/bin/bash
# Update Genesis to the latest release.
# Run from inside your Genesis repo directory.
#
# Usage: ./scripts/update.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GENESIS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$GENESIS_ROOT/.venv"

echo ""
echo "  Genesis Update"
echo "  ──────────────────────────────────────"

# ── Current state ─────────────────────────────────────────
OLD_TAG=$(git -C "$GENESIS_ROOT" describe --tags --abbrev=0 2>/dev/null || echo "untagged")
OLD_COMMIT=$(git -C "$GENESIS_ROOT" rev-parse --short HEAD)
echo "  Current: $OLD_TAG ($OLD_COMMIT)"
echo ""

# ── Pull ──────────────────────────────────────────────────
echo "--- Pulling latest ---"
git -C "$GENESIS_ROOT" pull origin main
NEW_TAG=$(git -C "$GENESIS_ROOT" describe --tags --abbrev=0 2>/dev/null || echo "untagged")
NEW_COMMIT=$(git -C "$GENESIS_ROOT" rev-parse --short HEAD)

if [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then
    echo "  Already up to date ($NEW_COMMIT)."
    echo ""
    echo "  Nothing to do."
    exit 0
fi
echo ""

# ── What changed ──────────────────────────────────────────
# Use OLD_COMMIT (exact pre-pull SHA) for precise diff — OLD_TAG may predate
# local commits that existed before the pull
echo "--- Changes ---"
git -C "$GENESIS_ROOT" log "${OLD_COMMIT}..HEAD" --oneline --no-merges | head -20 || true
echo ""

# ── Sync dependencies ─────────────────────────────────────
echo "--- Syncing dependencies ---"
if [[ ! -f "$VENV_DIR/bin/pip" ]]; then
    echo "  Venv not found — run ./scripts/bootstrap.sh first."
    exit 1
fi
"$VENV_DIR/bin/pip" install -e "$GENESIS_ROOT" --quiet
echo "  Dependencies synced"
echo ""

# ── Stop services before plugin sync ─────────────────────
# Stop first to avoid AZ reading partially-updated plugin files during rsync
echo "--- Stopping services for update ---"
WERE_RUNNING=()
for svc in agent-zero genesis-bridge; do
    if systemctl --user is-active --quiet "$svc.service" 2>/dev/null; then
        systemctl --user stop "$svc.service"
        WERE_RUNNING+=("$svc")
    fi
done
[[ ${#WERE_RUNNING[@]} -gt 0 ]] && echo "  Stopped: ${WERE_RUNNING[*]}" || echo "  No services were running"
echo ""

# ── Sync AZ plugins ───────────────────────────────────────
AZ_ROOT="$HOME/agent-zero"
if [[ -d "$AZ_ROOT/usr/plugins" && -d "$GENESIS_ROOT/az_plugins/genesis" ]]; then
    echo "--- Syncing AZ plugins ---"
    if command -v rsync &>/dev/null; then
        rsync -a --delete "$GENESIS_ROOT/az_plugins/genesis/" "$AZ_ROOT/usr/plugins/genesis/"
    else
        rm -rf "$AZ_ROOT/usr/plugins/genesis"
        cp -r "$GENESIS_ROOT/az_plugins/genesis" "$AZ_ROOT/usr/plugins/"
    fi
    echo "  AZ plugins synced"
    echo ""
fi

# ── Restart services ──────────────────────────────────────
if [[ ${#WERE_RUNNING[@]} -gt 0 ]]; then
    echo "--- Restarting services ---"
    RESTARTED=()
    FAILED=()

    for svc in "${WERE_RUNNING[@]}"; do
        systemctl --user start "$svc.service"
        RESTARTED+=("$svc")
    done

    # Verify services came up
    sleep 2
    for svc in "${RESTARTED[@]}"; do
        if systemctl --user is-active --quiet "$svc.service" 2>/dev/null; then
            echo "  OK: $svc"
        else
            echo "  FAILED: $svc — check: systemctl --user status $svc.service"
            FAILED+=("$svc")
        fi
    done

    if [[ ${#FAILED[@]} -gt 0 ]]; then
        echo ""
        echo "  WARNING: ${#FAILED[@]} service(s) failed to start. Check logs above."
        echo "  ──────────────────────────────────────"
        echo "  Updated: $OLD_TAG ($OLD_COMMIT) → $NEW_TAG ($NEW_COMMIT)"
        echo ""
        exit 1
    fi
    echo ""
fi

# ── Done ──────────────────────────────────────────────────
echo "  ──────────────────────────────────────"
echo "  Updated: $OLD_TAG ($OLD_COMMIT) → $NEW_TAG ($NEW_COMMIT)"
echo ""
