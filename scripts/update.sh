#!/bin/bash
# Update Genesis to the latest release.
# Run from inside your Genesis repo directory.
#
# Features:
#   - Pre-update backup (calls backup.sh)
#   - Rollback tag for safe revert on failure
#   - ERR trap wraps all mutating steps after rollback tag creation
#   - Idempotent bootstrap post-pull (config regen, systemd templates, hooks)
#   - Health verification with retry (fatal on failure)
#   - Migration failure is fatal and triggers rollback
#   - update_history table written on success + failure
#   - Writes failure context for CC-assisted recovery
#
# Usage: ./scripts/update.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GENESIS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$GENESIS_ROOT/.venv"
STARTED_AT="$(date -Iseconds)"

# Refuse to run from a worktree — pip install -e in bootstrap.sh would
# redirect system-wide imports and cause I/O death spiral.
if [[ "$GENESIS_ROOT" == *"/.claude/worktrees/"* ]] || \
   [[ "$GENESIS_ROOT" == *"/.worktrees/"* ]]; then
    echo "ERROR: update.sh must not run from a worktree."
    echo "       GENESIS_ROOT=$GENESIS_ROOT"
    echo "       Run from the main checkout instead."
    exit 1
fi

echo ""
echo "  Genesis Update"
echo "  ──────────────────────────────────────"

# ── Resolve upstream remote ────────────────────────────────
# Use the remote pointing to github_public_repo (e.g. 'public' for GENesis-AGI).
# Falls back to 'origin' if detection fails or genesis.env is unavailable.
_detect_update_remote() {
    local public_repo
    public_repo=$(
        "$VENV_DIR/bin/python" -c \
        "from genesis.env import github_public_repo; print(github_public_repo())" \
        2>/dev/null
    ) || public_repo="GENesis-AGI"
    local remote
    remote=$(git -C "$GENESIS_ROOT" remote -v 2>/dev/null \
        | awk "/$public_repo.*fetch/{print \$1; exit}")
    echo "${remote:-origin}"
}
UPDATE_REMOTE="$(_detect_update_remote)"
echo "  Update remote: $UPDATE_REMOTE"

# ── Current state ─────────────────────────────────────────
ORIGINAL_BRANCH=$(git -C "$GENESIS_ROOT" symbolic-ref --short HEAD 2>/dev/null || echo "main")
OLD_TAG=$(git -C "$GENESIS_ROOT" describe --tags --abbrev=0 2>/dev/null || echo "untagged")
OLD_COMMIT=$(git -C "$GENESIS_ROOT" rev-parse --short HEAD)
NEW_TAG="$OLD_TAG"
NEW_COMMIT="$OLD_COMMIT"
echo "  Branch:  $ORIGINAL_BRANCH"
echo "  Current: $OLD_TAG ($OLD_COMMIT)"
echo ""

# ── Pre-update backup ────────────────────────────────────
if [ -x "$GENESIS_ROOT/scripts/backup.sh" ]; then
    echo "--- Pre-update backup ---"
    if "$GENESIS_ROOT/scripts/backup.sh" 2>&1 | tail -3; then
        echo "  Backup complete"
    else
        echo "  WARNING: backup failed (continuing anyway)"
    fi
    echo ""
fi

# ── Rollback tag ─────────────────────────────────────────
ROLLBACK_TAG="pre-update-$(date +%Y%m%d-%H%M%S)"
git -C "$GENESIS_ROOT" tag "$ROLLBACK_TAG"
echo "  Rollback tag: $ROLLBACK_TAG"
echo ""

# ── Stop services for update ──────────────────────────────
echo "--- Stopping services for update ---"
WERE_RUNNING=()
for svc in genesis-server genesis-bridge; do
    if systemctl --user is-active --quiet "$svc.service" 2>/dev/null; then
        systemctl --user stop "$svc.service" || true  # stop failure shouldn't block rollback setup
        WERE_RUNNING+=("$svc")
    fi
done
[[ ${#WERE_RUNNING[@]} -gt 0 ]] && echo "  Stopped: ${WERE_RUNNING[*]}" || echo "  No services were running"
echo ""

# ── update_history helper ────────────────────────────────
# Records an entry in update_history. Silently no-ops if the table
# doesn't exist yet (first update before migration 0001 has run).
_record_update_history() {
    local status="$1"           # success | failed | rolled_back
    local reason="${2:-}"
    local degraded="${3:-}"
    local db_path="$GENESIS_ROOT/data/genesis.db"
    [ -f "$db_path" ] || return 0
    [ -x "$VENV_DIR/bin/python" ] || return 0

    # Run the insert in Python for parameterized SQL. The inline script
    # distinguishes three exit paths:
    #   0 — inserted OK
    #   2 — table missing (first update before 0001 ran) — expected, silent
    #   1 — any other error (logged to stderr + bash warns)
    # Do NOT pipe stderr to /dev/null — silencing failures is the
    # antipattern we're fixing. Only the "table missing" case is
    # allowed to be silent.
    # Note: we use `|| py_rc=$?` pattern because set -e otherwise triggers
    # on any non-zero $() assignment (including the expected rc=2 for
    # "table missing" case).
    local py_output=""
    local py_rc=0
    py_output=$(
        GH_STATUS="$status" \
        GH_REASON="$reason" \
        GH_DEGRADED="$degraded" \
        GH_DB_PATH="$db_path" \
        GH_OLD_TAG="$OLD_TAG" \
        GH_NEW_TAG="$NEW_TAG" \
        GH_OLD_COMMIT="$OLD_COMMIT" \
        GH_NEW_COMMIT="$NEW_COMMIT" \
        GH_ROLLBACK_TAG="$ROLLBACK_TAG" \
        GH_STARTED_AT="$STARTED_AT" \
        "$VENV_DIR/bin/python" - <<'PYEOF' 2>&1
import os
import sqlite3
import sys
import uuid
from datetime import UTC, datetime

try:
    con = sqlite3.connect(os.environ["GH_DB_PATH"], timeout=5.0)
    con.execute(
        "INSERT INTO update_history "
        "(id, old_tag, new_tag, old_commit, new_commit, status, rollback_tag, "
        "failure_reason, degraded_subsystems, started_at, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            os.environ["GH_OLD_TAG"],
            os.environ["GH_NEW_TAG"],
            os.environ["GH_OLD_COMMIT"],
            os.environ["GH_NEW_COMMIT"],
            os.environ["GH_STATUS"],
            os.environ.get("GH_ROLLBACK_TAG") or None,
            os.environ.get("GH_REASON") or None,
            os.environ.get("GH_DEGRADED") or None,
            os.environ["GH_STARTED_AT"],
            datetime.now(UTC).isoformat(),
        ),
    )
    con.commit()
    con.close()
    sys.exit(0)
except sqlite3.OperationalError as exc:
    msg = str(exc).lower()
    if "no such table" in msg:
        # Expected on the very first update — 0001 hasn't run yet.
        sys.exit(2)
    print(f"update_history insert failed (OperationalError): {exc}", file=sys.stderr)
    sys.exit(1)
except Exception as exc:
    print(f"update_history insert failed ({type(exc).__name__}): {exc}", file=sys.stderr)
    sys.exit(1)
PYEOF
    ) || py_rc=$?
    case "$py_rc" in
        0) : ;;  # success
        2) : ;;  # table missing — expected
        *)
            echo "  WARNING: failed to record update_history entry:" >&2
            echo "    $py_output" >&2
            ;;
    esac
    return 0
}

# ── Rollback helper function ─────────────────────────────
_do_rollback() {
    local reason="$1"
    local degraded="${2:-}"

    # Disarm the ERR trap to prevent recursive rollback
    trap - ERR

    echo ""
    echo "  UPDATE FAILED — $reason"
    echo "  Rolling back to $ROLLBACK_TAG..."

    # Stop any running services first
    systemctl --user stop genesis-server genesis-bridge 2>/dev/null || true

    # Restore the original branch, then reset it to the rollback tag.
    # This keeps us on a named branch (not detached HEAD) at the pre-update state.
    local checkout_ok=true
    if ! git -C "$GENESIS_ROOT" checkout "$ORIGINAL_BRANCH" 2>&1; then
        echo "  CRITICAL: failed to checkout $ORIGINAL_BRANCH"
        checkout_ok=false
    fi
    if [ "$checkout_ok" = "true" ]; then
        if ! git -C "$GENESIS_ROOT" reset --hard "$ROLLBACK_TAG" 2>&1; then
            echo "  CRITICAL: failed to reset $ORIGINAL_BRANCH to $ROLLBACK_TAG"
            checkout_ok=false
        fi
    fi

    # Re-sync dependencies against the rolled-back code
    local pip_ok=true
    if ! "$VENV_DIR/bin/pip" install -e "$GENESIS_ROOT" --quiet 2>&1 | tail -1; then
        echo "  CRITICAL: pip install failed during rollback"
        pip_ok=false
    fi

    # Restart services with old code
    for svc in "${WERE_RUNNING[@]}"; do
        systemctl --user start "$svc.service" 2>/dev/null || \
            echo "  CRITICAL: failed to restart $svc"
    done

    if [ "$checkout_ok" = "true" ] && [ "$pip_ok" = "true" ]; then
        echo "  Rolled back to $ROLLBACK_TAG"
        _record_update_history "rolled_back" "$reason" "$degraded"
    else
        echo "  ROLLBACK INCOMPLETE — manual intervention required"
        echo "  Last known good state: $OLD_TAG ($OLD_COMMIT) on $ORIGINAL_BRANCH"
        _record_update_history "failed" "$reason (rollback incomplete)" "$degraded"
    fi

    echo ""
    echo "  To diagnose: discuss with Claude Code"
    echo "  Context: Update from $OLD_TAG to $NEW_TAG failed."
    echo "  Reason: $reason"
    [ -n "$degraded" ] && echo "  Degraded subsystems: $degraded"

    # Write failure context for CC to pick up
    mkdir -p "$HOME/.genesis"
    cat > "$HOME/.genesis/last_update_failure.json" << FAILEOF
{
    "old_tag": "$OLD_TAG",
    "new_tag": "$NEW_TAG",
    "old_commit": "$OLD_COMMIT",
    "new_commit": "$NEW_COMMIT",
    "rollback_tag": "$ROLLBACK_TAG",
    "reason": "$reason",
    "degraded_subsystems": "$degraded",
    "original_branch": "$ORIGINAL_BRANCH",
    "rollback_complete": $([ "$checkout_ok" = "true" ] && [ "$pip_ok" = "true" ] && echo true || echo false),
    "timestamp": "$(date -Iseconds)"
}
FAILEOF

    echo ""
    echo "  ──────────────────────────────────────"
    echo "  Rolled back: $OLD_TAG ($OLD_COMMIT) on $ORIGINAL_BRANCH"
    echo ""
}

# ── Install ERR trap — catches any unhandled failure after this point ─
# Uses $BASH_COMMAND to report which command failed.
_on_err() {
    local exit_code=$?
    _do_rollback "command failed (exit $exit_code): $BASH_COMMAND"
    exit 1
}
trap _on_err ERR

# ── Pull ──────────────────────────────────────────────────
echo "--- Pulling latest ---"
git -C "$GENESIS_ROOT" pull --rebase "$UPDATE_REMOTE" main
NEW_TAG=$(git -C "$GENESIS_ROOT" describe --tags --abbrev=0 2>/dev/null || echo "untagged")
NEW_COMMIT=$(git -C "$GENESIS_ROOT" rev-parse --short HEAD)

if [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then
    echo "  Already up to date ($NEW_COMMIT)."
    # Clean up unnecessary rollback tag and disarm trap
    trap - ERR
    git -C "$GENESIS_ROOT" tag -d "$ROLLBACK_TAG" 2>/dev/null || true
    # Restart services that we stopped (if any)
    for svc in "${WERE_RUNNING[@]}"; do
        systemctl --user start "$svc.service" 2>/dev/null || true
    done
    echo ""
    echo "  Nothing to do."
    exit 0
fi
echo ""

# ── What changed ──────────────────────────────────────────
echo "--- Changes ---"
git -C "$GENESIS_ROOT" log "${OLD_COMMIT}..HEAD" --oneline --no-merges | head -20 || true
echo ""

# ── Run bootstrap (idempotent — handles deps, configs, hooks, systemd) ─
echo "--- Running bootstrap ---"
"$GENESIS_ROOT/scripts/bootstrap.sh" 2>&1 | tail -10
echo "  Bootstrap complete"
echo ""

# ── Verify Genesis is importable ──────────────────────────
if ! "$VENV_DIR/bin/python" -c "from genesis.runtime import GenesisRuntime" 2>/dev/null; then
    _do_rollback "Genesis not importable after bootstrap"
    exit 1
fi
echo "  Genesis importable: OK"
echo ""

# ── Run migrations (fatal on failure) ─────────────────────
if "$VENV_DIR/bin/python" -c "import genesis.db.migrations" 2>/dev/null; then
    echo "--- Running migrations ---"
    if ! "$VENV_DIR/bin/python" -m genesis.db.migrations --apply 2>&1 | tail -10; then
        _do_rollback "migration runner failed"
        exit 1
    fi
    echo "  Migrations complete"
    echo ""
fi

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

# ── Restart services ──────────────────────────────────────
if [[ ${#WERE_RUNNING[@]} -gt 0 ]]; then
    echo "--- Restarting services ---"

    # Reload systemd in case templates changed during bootstrap
    systemctl --user daemon-reload 2>/dev/null || true

    for svc in "${WERE_RUNNING[@]}"; do
        if ! systemctl --user start "$svc.service"; then
            _do_rollback "failed to start $svc after update"
            exit 1
        fi
    done

    # ── Health verification with retry (FATAL on failure) ─
    echo "--- Verifying system health ---"
    HEALTH_OK=false
    DEGRADED=""

    for attempt in 1 2 3; do
        sleep 5
        if curl -sf http://localhost:5000/api/genesis/health > /dev/null 2>&1; then
            echo "  OK: Genesis health endpoint responding (attempt $attempt)"
            HEALTH_OK=true
            break
        fi
        echo "  Attempt $attempt: health endpoint not responding..."
    done

    if [ "$HEALTH_OK" = "true" ]; then
        # Check for failed subsystems
        DEGRADED=$(curl -sf http://localhost:5000/api/genesis/health 2>/dev/null | \
            "$VENV_DIR/bin/python" -c "
import sys, json
try:
    d = json.load(sys.stdin)
    failed = [k for k,v in d.get('subsystems',{}).items() if v.get('status') == 'failed']
    print(' '.join(failed))
except Exception:
    pass
" 2>/dev/null || true)
        if [ -n "$DEGRADED" ]; then
            echo "  Degraded subsystems: $DEGRADED"
            _do_rollback "subsystems failed after update: $DEGRADED" "$DEGRADED"
            exit 1
        fi
    fi

    # Verify services are active
    SVC_FAILED=()
    for svc in "${WERE_RUNNING[@]}"; do
        if systemctl --user is-active --quiet "$svc.service" 2>/dev/null; then
            echo "  OK: $svc"
        else
            echo "  FAILED: $svc — check: systemctl --user status $svc.service"
            SVC_FAILED+=("$svc")
        fi
    done

    if [[ ${#SVC_FAILED[@]} -gt 0 ]]; then
        _do_rollback "${#SVC_FAILED[@]} service(s) failed to start: ${SVC_FAILED[*]}" "$DEGRADED"
        exit 1
    fi

    if [ "$HEALTH_OK" = "false" ]; then
        _do_rollback "health endpoint did not respond after 3 attempts (15s)"
        exit 1
    fi
    echo ""
fi

# ── Success: disarm trap ──────────────────────────────────
trap - ERR

# ── Update Guardian on host VM (if configured) ──────────
GUARDIAN_CONFIG="$HOME/.genesis/guardian_remote.yaml"
if [ -f "$GUARDIAN_CONFIG" ]; then
    HOST_IP=$("$VENV_DIR/bin/python" -c "
import yaml, pathlib
cfg = yaml.safe_load(pathlib.Path('$GUARDIAN_CONFIG').read_text())
print(cfg.get('host_ip', ''))
" 2>/dev/null || true)
    HOST_USER=$("$VENV_DIR/bin/python" -c "
import yaml, pathlib
cfg = yaml.safe_load(pathlib.Path('$GUARDIAN_CONFIG').read_text())
print(cfg.get('host_user', 'ubuntu'))
" 2>/dev/null || echo "ubuntu")
    SSH_KEY="$HOME/.ssh/genesis_guardian_ed25519"

    if [ -n "$HOST_IP" ] && [ -f "$SSH_KEY" ]; then
        echo "--- Updating Guardian on host VM ---"
        if ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
               "${HOST_USER}@${HOST_IP}" update 2>&1; then
            echo "  Guardian updated"
        else
            echo "  Guardian update failed (non-fatal)"
        fi
        echo ""
    fi
fi

# ── Clear update failure file on success ──────────────────
if [ -f "$HOME/.genesis/last_update_failure.json" ]; then
    rm -f "$HOME/.genesis/last_update_failure.json"
    echo "  Cleared previous update failure context"
fi

# ── Record success in update_history ─────────────────────
_record_update_history "success" "" ""

# ── Done ──────────────────────────────────────────────────
echo "  ──────────────────────────────────────"
echo "  Updated: $OLD_TAG ($OLD_COMMIT) → $NEW_TAG ($NEW_COMMIT)"
echo ""
