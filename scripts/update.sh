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
git -C "$GENESIS_ROOT" pull --rebase origin main
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
"$VENV_DIR/bin/pip" install -e "$GENESIS_ROOT" --quiet 2>&1 | tail -1 || true
if ! "$VENV_DIR/bin/python" -c "from genesis.runtime import GenesisRuntime" 2>/dev/null; then
    echo "  FAIL: pip install completed but Genesis is not importable."
    echo "  Re-run: $VENV_DIR/bin/pip install -e $GENESIS_ROOT --verbose"
    # Attempt to restart services before exiting — they may have been stopped
    systemctl --user start genesis-server 2>/dev/null || true
    exit 1
fi
echo "  Dependencies synced"
echo ""

# ── Fix Network Identity (if unresolved template vars) ────
_claude_md="$GENESIS_ROOT/CLAUDE.md"
if grep -qE '\$\{|:-localhost\}' "$_claude_md" 2>/dev/null; then
    echo "--- Fixing Network Identity in CLAUDE.md ---"
    _c_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    _c_ipv6=$(ip -6 addr show scope global 2>/dev/null | grep -oP 'inet6 \K[^ /]+' | head -1 || true)
    _host_ip=$("$VENV_DIR/bin/python" -c "
import yaml, pathlib
p = pathlib.Path.home() / '.genesis' / 'guardian_remote.yaml'
if p.exists():
    cfg = yaml.safe_load(p.read_text())
    print(cfg.get('host_ip', ''))
" 2>/dev/null || true)
    [ -z "$_host_ip" ] && _host_ip=$(ip route | grep default | awk '{print $3}' || true)

    sed -i '/^## Network Identity/,$d' "$_claude_md"
    {
        echo ""
        echo "## Network Identity"
        echo ""
        printf -- "- **Container IP**: %s" "${_c_ip:-localhost}"
        [ -n "$_c_ipv6" ] && printf " (v6: %s)" "$_c_ipv6"
        echo ""
        printf -- "- **Host VM IP**: %s\n" "${_host_ip:-localhost}"
        printf -- "- **Dashboard**: http://%s:5000 (via proxy device)\n" "${_host_ip:-localhost}"
    } >> "$_claude_md"
    echo "  Network identity updated in CLAUDE.md"
    echo ""
fi

# ── Stop services for update ──────────────────────────────
echo "--- Stopping services for update ---"
WERE_RUNNING=()
for svc in genesis-server genesis-bridge; do
    if systemctl --user is-active --quiet "$svc.service" 2>/dev/null; then
        systemctl --user stop "$svc.service"
        WERE_RUNNING+=("$svc")
    fi
done
[[ ${#WERE_RUNNING[@]} -gt 0 ]] && echo "  Stopped: ${WERE_RUNNING[*]}" || echo "  No services were running"
echo ""

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
