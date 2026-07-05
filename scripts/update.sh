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
# Usage: ./scripts/update.sh [--post-merge]
#   --post-merge  Skip fetch/merge (code already merged by CC conflict resolution);
#                 run only bootstrap, migrations, health check, and service restart.

set -euo pipefail

# ── Copy-to-temp guard ──────────────────────────────────
# The update script may update itself during git merge, which would corrupt
# the running process. Industry standard (Chrome, Homebrew, Windows Update):
# copy to temp, exec from there, so the original can be safely overwritten.
if [ "${GENESIS_UPDATE_FROM_TEMP:-}" != "1" ]; then
    mkdir -p "$HOME/tmp"
    TEMP_COPY=$(mktemp "$HOME/tmp/genesis-update-XXXXXX.sh")
    cp "$0" "$TEMP_COPY"
    chmod +x "$TEMP_COPY"
    export GENESIS_UPDATE_FROM_TEMP=1
    # Pass original script dir so GENESIS_ROOT resolves correctly
    export GENESIS_UPDATE_ORIG_DIR="$(cd "$(dirname "$0")/.." && pwd)"
    exec "$TEMP_COPY" "$@"
fi
# Running from temp copy — clean up on exit
trap 'rm -f "${BASH_SOURCE[0]}" 2>/dev/null' EXIT

# ── Ensure systemctl --user works ───────────────────────
# CC sessions lack D-Bus env vars, causing systemctl --user to fail silently
# and triggering nohup fallback. Same fix as genesis.util.systemd.systemctl_env().
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

# ── Flag parsing ─────────────────────────────────────────
POST_MERGE=false
for _arg in "$@"; do
    [[ "$_arg" == "--post-merge" ]] && POST_MERGE=true
done

GENESIS_ROOT="${GENESIS_UPDATE_ORIG_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
SCRIPT_DIR="$GENESIS_ROOT/scripts"
VENV_DIR="$GENESIS_ROOT/.venv"
STARTED_AT="$(date -Iseconds)"
STATE_FILE="$HOME/.genesis/update_state.json"

# ── Update state file helper ────────────────────────────
# Written at each phase boundary so crash recovery knows where we stopped.
_write_state() {
    local phase="$1"
    mkdir -p "$HOME/.genesis"
    cat > "$STATE_FILE" << SEOF
{
    "phase": "$phase",
    "rollback_tag": "${ROLLBACK_TAG:-}",
    "old_tag": "${OLD_TAG:-}",
    "old_commit": "${OLD_COMMIT:-}",
    "started_at": "$STARTED_AT",
    "pid": $$,
    "services_stopped": [$(printf '"%s",' "${WERE_RUNNING[@]:-}" | sed 's/,$//')],
    "timestamp": "$(date -Iseconds)"
}
SEOF
}

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
OLD_TAG=$(git -C "$GENESIS_ROOT" describe --tags --match 'v*' --abbrev=0 2>/dev/null || echo "untagged")
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

# ── Dirty-tree guard ─────────────────────────────────────
# Abort before touching anything if tracked files are modified.
# Untracked files (^??) are excluded — merge/reset never touches them.
# git reset --hard in _do_rollback would silently discard uncommitted work.
#
# ── Deploy-target sync: guardian redeploy + host Node/CC + container CC ──
# Extracted into a function so it runs on BOTH paths: the normal post-update
# path AND the "Already up to date" path. Drift healing (pin alignment on the
# host and container, guardian code redeploy) must never depend on whether THIS
# run's git merge happened to bring commits — a pin bump pulled manually, or a
# previously failed sync, still needs healing on a no-op run.
# Sets the global HOST_CC_DEGRADED (consumed by _record_update_history).
_sync_deploy_targets() {
    # Accumulates any host-side alignment failure so it is recorded as a degraded
    # subsystem in update_history (surfaced by the dashboard) rather than silently
    # skipped. Empty = host fully aligned (or no guardian configured).
    HOST_CC_DEGRADED=""

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
            # Check if Guardian-relevant paths changed in this update
            GUARDIAN_PATHS="src/genesis/guardian src/genesis/util src/genesis/env.py src/genesis/observability src/genesis/db config/guardian-claude.md pyproject.toml scripts/install_guardian.sh scripts/guardian-gateway.sh"
            DEPLOY_HASH=$(git -C "$GENESIS_ROOT" rev-parse --short HEAD)

            if ! git -C "$GENESIS_ROOT" diff --quiet "$OLD_COMMIT" HEAD -- $GUARDIAN_PATHS 2>/dev/null; then
                echo "--- Guardian-relevant paths changed — redeploying to host ---"
                # Try push-based redeploy (new gateway verb)
                # Archive excludes config/guardian.yaml (host-specific, generated by installer)
                ARCHIVE_CMD=(git -C "$GENESIS_ROOT" archive HEAD -- src/ scripts/ pyproject.toml config/guardian-claude.md)
                if "${ARCHIVE_CMD[@]}" | \
                   ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=30 \
                       "${HOST_USER}@${HOST_IP}" "redeploy $DEPLOY_HASH" 2>/dev/null; then
                    echo "  Guardian redeployed ($DEPLOY_HASH)"
                else
                    # Fallback: old gateway doesn't know 'redeploy' — use 'update'
                    # to install new gateway, then retry redeploy
                    echo "  Redeploy not available — falling back to update + retry"
                    if ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
                           "${HOST_USER}@${HOST_IP}" update 2>&1; then
                        echo "  Guardian updated via git pull — retrying redeploy..."
                        # Gateway is a static file invoked fresh per SSH connection.
                        # After update writes the new version, next ssh uses it.
                        if "${ARCHIVE_CMD[@]}" | \
                           ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=30 \
                               "${HOST_USER}@${HOST_IP}" "redeploy $DEPLOY_HASH" 2>/dev/null; then
                            echo "  Guardian redeployed on retry ($DEPLOY_HASH)"
                        else
                            echo "  Guardian redeploy retry failed (non-fatal)"
                        fi
                    else
                        echo "  Guardian update failed (non-fatal)"
                    fi
                fi
            else
                echo "--- No Guardian-relevant changes — skipping host redeploy ---"
            fi

            # ── Sync host Node.js + Claude Code to the pinned versions (WS-16) ──
            # The host runs `claude -p` for Guardian's intelligent diagnosis/recovery
            # (guardian/diagnosis.py) — the highest-stakes CC call in the system: when
            # it fires, Genesis is down and, without CC's judgment, no programmatic
            # recovery is safe. So the host must carry a WORKING Claude Code at the
            # container pin, which in turn needs a compatible Node.js major (CC 2.1.198
            # needs node >=22). This block keeps BOTH aligned. Non-fatal, but NOT
            # silent: any alignment failure is recorded as a degraded subsystem
            # (guardian_host_*) in update_history so the dashboard/health surface it,
            # instead of the old misleading "gateway unreachable" skip.
            _cc_env="$SCRIPT_DIR/lib/cc_version.sh"
            if [ -f "$_cc_env" ]; then
                # Sync the host to the REPO pins, never an inherited override.
                unset CC_VERSION NODE_MAJOR
                # shellcheck source=/dev/null
                source "$_cc_env"
            else
                echo "  WARNING: $_cc_env missing — skipping host Node/CC sync"
            fi

            # One `version` call yields both node_version and cc_version.
            HOST_VER_RAW="$(ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
                "${HOST_USER}@${HOST_IP}" version 2>/dev/null || true)"

            if [ -z "$HOST_VER_RAW" ]; then
                # Genuinely could not reach/parse the gateway — DISTINCT from "CC
                # absent" (the conflation the old message got wrong).
                echo "  Host gateway unreachable (no version response) — skipping Node/CC sync (non-fatal)"
                HOST_CC_DEGRADED="guardian_host_unreachable"
            else
                HOST_NODE_MAJOR="$(printf '%s' "$HOST_VER_RAW" \
                    | grep -oE '"node_version": "v[0-9]+' | grep -oE '[0-9]+' || true)"
                HOST_CC="$(printf '%s' "$HOST_VER_RAW" \
                    | grep -oE '"cc_version": "[0-9]+\.[0-9]+\.[0-9]+' \
                    | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)"

                # ── Node.js major sync (prerequisite for CC) ──
                if printf '%s' "${NODE_MAJOR:-}" | grep -qE '^[0-9]{1,2}$'; then
                    if [ "$HOST_NODE_MAJOR" = "$NODE_MAJOR" ]; then
                        echo "  Host Node.js already at major $NODE_MAJOR — no Node sync needed"
                    else
                        echo "--- Host Node.js: ${HOST_NODE_MAJOR:-unknown} → syncing to major $NODE_MAJOR ---"
                        # 600s: NodeSource repo-add + apt install is heavier than an
                        # npm install (update-cc uses 300s); bounds a hung dpkg lock.
                        if timeout 600 ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=30 \
                            "${HOST_USER}@${HOST_IP}" "update-node $NODE_MAJOR" 2>&1; then
                            echo "  Host Node.js updated to major $NODE_MAJOR"
                            HOST_NODE_MAJOR="$NODE_MAJOR"
                        else
                            echo "  WARNING: Host Node.js sync failed — CC install will likely fail (host stays on ${HOST_NODE_MAJOR:-unknown})"
                            HOST_CC_DEGRADED="${HOST_CC_DEGRADED:+$HOST_CC_DEGRADED,}guardian_host_node"
                        fi
                    fi
                fi

                # ── Claude Code sync: absence => INSTALL, drift => update ──
                if printf '%s' "${CC_VERSION:-}" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
                    if [ -z "$HOST_CC" ]; then
                        # cc_version was "unavailable"/unparseable → CC is NOT installed
                        # on the host. INSTALL it (do not skip) — this is the exact case
                        # the old code silently ignored, leaving Guardian's recovery
                        # brain offline.
                        echo "--- Host Claude Code not installed — installing $CC_VERSION ---"
                        if timeout 300 ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=30 \
                            "${HOST_USER}@${HOST_IP}" "update-cc $CC_VERSION" 2>&1; then
                            echo "  Host Claude Code installed ($CC_VERSION)"
                        else
                            echo "  WARNING: Host Claude Code install FAILED — Guardian intelligent recovery is OFFLINE"
                            HOST_CC_DEGRADED="${HOST_CC_DEGRADED:+$HOST_CC_DEGRADED,}guardian_host_cc"
                        fi
                    elif [ "$HOST_CC" = "$CC_VERSION" ]; then
                        echo "  Host Claude Code already at pin ($CC_VERSION) — no CC sync needed"
                    else
                        echo "--- Host Claude Code drift: $HOST_CC → syncing to $CC_VERSION ---"
                        if timeout 300 ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=30 \
                            "${HOST_USER}@${HOST_IP}" "update-cc $CC_VERSION" 2>&1; then
                            echo "  Host Claude Code updated to $CC_VERSION"
                        else
                            echo "  WARNING: Host Claude Code sync failed — host remains on $HOST_CC"
                            HOST_CC_DEGRADED="${HOST_CC_DEGRADED:+$HOST_CC_DEGRADED,}guardian_host_cc"
                        fi
                    fi
                fi
            fi
            echo ""
        fi
    fi

    # ── Sync CONTAINER Claude Code to the pinned version ──────
    # The guardian block above syncs the HOST's CC. This aligns the CONTAINER's own
    # Claude Code (update.sh runs inside the container) so a pin bump reaches the
    # container with zero user action. UNCONDITIONAL — not gated on guardian config,
    # so guardian-less installs are covered too. Non-fatal (`|| true`): `set -e` is
    # active here (ERR trap already disarmed), and a CC install hiccup must never
    # abort an update after git-pull/migrations have run.
    _cc_env="$SCRIPT_DIR/lib/cc_version.sh"
    if [ -f "$_cc_env" ]; then
        echo "--- Syncing container Claude Code to pin ---"
        unset CC_VERSION            # repo pin must win over any inherited override
        # shellcheck source=/dev/null
        source "$_cc_env"
        cc_ensure_local || true
        cc_shadow_scan || true
    else
        echo "  WARNING: $_cc_env missing — skipping container CC sync"
    fi
    echo ""
}

# EXCEPTION — known-ephemeral tracked files (EPHEMERAL_DIRTY_RE): tracked files
# that are routinely rewritten in place and are safe to ignore. They regenerate
# themselves, and local edits to them are discarded right before the merge
# (see "--- Fetching latest ---" below) so an incoming change to one of them
# never aborts the merge with "local changes would be overwritten". Today:
#   - top-level `AGENTS.md` (GitNexus rewrites its auto-stat block)
#   - `config/procedure_triggers.yaml` (the L1 trigger cache rewrites it in
#     place). NOTE: this is now .gitignored and regenerated per-install at
#     bootstrap (`seed_procedures.py`); the entry only matters transitionally for
#     installs that still track it. AGENTS.md is the remaining tracked-ephemeral.
# These no longer block an update; REAL tracked changes still abort. Each
# alternative anchors the exact porcelain path (a single space precedes it), so
# only these exact paths are excused — e.g. `src/AGENTS.md` or
# `src/config/procedure_triggers.yaml` would still abort.
# (`.claude/settings.local.json` is install-local and belongs untracked; PR #792
# accidentally re-tracked it, which made every live install permanently dirty and
# blocked update.sh. This release de-tracks it again — the entry below excuses it
# transitionally, and the backup/restore pair around the merge preserves the live
# copy through the upstream deletion. Steady state: untracked + .gitignored.)
EPHEMERAL_DIRTY_RE=' AGENTS\.md$| config/procedure_triggers\.yaml$| \.claude/settings\.local\.json$'
if [[ "$POST_MERGE" == "false" ]]; then
    DIRTY_FILES=$(git -C "$GENESIS_ROOT" status --porcelain 2>/dev/null \
        | grep -v "^??" \
        | grep -vE "$EPHEMERAL_DIRTY_RE" || true)
    if [[ -n "$DIRTY_FILES" ]]; then
        echo "ERROR: Working tree has uncommitted changes. Clean them up first:"
        echo "$DIRTY_FILES"
        echo ""
        # Mid-merge state (UU/AA entries) needs abort, not stash/commit
        if git -C "$GENESIS_ROOT" rev-parse --verify MERGE_HEAD &>/dev/null; then
            echo "  Repo is mid-merge. Run: git merge --abort"
        else
            echo "  git stash        # save and restore after update"
            echo "  git add -p && git commit -m 'chore: save local changes'  # commit"
        fi
        exit 1
    fi
fi

# ── Rollback tag ─────────────────────────────────────────
ROLLBACK_TAG="pre-update-$(date +%Y%m%d-%H%M%S)"
if [[ "$POST_MERGE" == "true" ]] && [ -f "$STATE_FILE" ]; then
    # In post-merge mode, reuse the rollback tag from the initial update.sh run
    # so rollback goes to pre-merge code, not the merged code.
    _saved_rt=$(
        GH_STATE_FILE="$STATE_FILE" \
        "$VENV_DIR/bin/python" -c \
        "import json, os; print(json.load(open(os.environ['GH_STATE_FILE'])).get('rollback_tag',''))" \
        2>/dev/null
    ) || _saved_rt=""
    if [ -n "$_saved_rt" ] && git -C "$GENESIS_ROOT" rev-parse "$_saved_rt" >/dev/null 2>&1; then
        ROLLBACK_TAG="$_saved_rt"
        echo "  Post-merge mode: reusing rollback tag $ROLLBACK_TAG"
    else
        git -C "$GENESIS_ROOT" tag "$ROLLBACK_TAG"
        echo "  Post-merge mode: created fallback rollback tag $ROLLBACK_TAG"
    fi
    # Recover OLD_TAG/OLD_COMMIT from state file for correct update_history.
    _saved_old_tag=$(
        GH_STATE_FILE="$STATE_FILE" \
        "$VENV_DIR/bin/python" -c \
        "import json, os; print(json.load(open(os.environ['GH_STATE_FILE'])).get('old_tag',''))" \
        2>/dev/null
    ) || true
    [ -n "${_saved_old_tag:-}" ] && OLD_TAG="$_saved_old_tag"
    _saved_old_commit=$(
        GH_STATE_FILE="$STATE_FILE" \
        "$VENV_DIR/bin/python" -c \
        "import json, os; print(json.load(open(os.environ['GH_STATE_FILE'])).get('old_commit',''))" \
        2>/dev/null
    ) || true
    [ -n "${_saved_old_commit:-}" ] && OLD_COMMIT="$_saved_old_commit"
else
    git -C "$GENESIS_ROOT" tag "$ROLLBACK_TAG"
    echo "  Rollback tag: $ROLLBACK_TAG"
fi
echo ""

_write_state "fetching"

# ── Service stop/start helpers ───────────────────────────
# Works with both systemd and bare-process environments.
_stop_genesis_server() {
    # Try systemctl first (works when D-Bus session bus is available)
    if systemctl --user is-active --quiet genesis-server.service 2>/dev/null; then
        systemctl --user stop genesis-server.service 2>/dev/null && return 0
    fi
    # Fallback: read PID from fcntl lock file
    local lock_file="$HOME/.genesis/genesis-server.lock"
    if [ -f "$lock_file" ]; then
        local pid
        pid=$(tr -d '\0' < "$lock_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null
            # Wait up to 10s for graceful shutdown
            for i in $(seq 1 20); do
                kill -0 "$pid" 2>/dev/null || return 0
                sleep 0.5
            done
            kill -KILL "$pid" 2>/dev/null || true
            return 0
        fi
    fi
    # Last resort: pkill by command pattern
    pkill -TERM -f "python -m genesis serve" 2>/dev/null || true
    sleep 1
}

_ensure_server_down() {
    # Guarantee genesis-server is stopped AND stays stopped before mutating the
    # repo/DB. systemd's Restart=on-failure can resurrect the server after a
    # kill-based stop (the kill reads as a failure, arming a RestartSec timer); an
    # explicit `systemctl stop` transitions the unit to inactive/dead and DISARMS
    # that timer (on-failure only fires from active/running). Without this, an
    # auto-restarted STALE-code process runs during the merge + migration window —
    # the bug that shipped a deploy whose running process never loaded the new code.
    systemctl --user stop genesis-server.service 2>/dev/null || true
    for _ in $(seq 1 20); do
        if ! systemctl --user is-active --quiet genesis-server.service 2>/dev/null \
           && ! pgrep -f "python -m genesis serve" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.5
    done
    # The restart timer may have fired between the stop and the poll — stop again,
    # then hard-kill as a last resort.
    systemctl --user stop genesis-server.service 2>/dev/null || true
    pkill -KILL -f "python -m genesis serve" 2>/dev/null || true
    sleep 1
    if pgrep -f "python -m genesis serve" >/dev/null 2>&1; then
        echo "  ERROR: genesis-server could not be stopped"
        return 1
    fi
    return 0
}

_start_genesis_server() {
    # Use `restart` (NOT `start`): systemd's Restart=on-failure can resurrect a
    # STALE-code instance mid-update (a kill-based stop is seen as a failure and
    # arms a RestartSec timer). `start` is a no-op on that already-running instance
    # and would silently leave the OLD code live after the update. `restart` always
    # stop+starts from the current on-disk code, and still starts the unit cleanly
    # when it is already stopped.
    if systemctl --user restart genesis-server.service 2>&1; then
        echo "  Started genesis-server via systemd"
        return 0
    fi
    # Fallback: start directly — DEGRADED MODE
    # This bypasses systemd monitoring, so health dashboard will show red.
    echo "  WARNING: systemctl --user restart failed — falling back to direct start (degraded)"
    echo "  Health monitoring will not work correctly. Run: systemctl --user restart genesis-server.service"
    nohup "$VENV_DIR/bin/python" -m genesis serve --host 0.0.0.0 --port 5000 \
        >> "$HOME/.genesis/logs/genesis-server.log" 2>&1 &
    echo "  Started genesis-server in degraded mode (pid $!)"
    # Write marker so dashboard can detect degraded mode
    echo "nohup" > "$HOME/.genesis/server-start-mode"
}

# ── Pre-update DB snapshot ────────────────────────────────
# Flush the WAL and create a clean backup before stopping services.
# If the server is killed mid-write during the update, this backup
# enables recovery without data loss.
DB_FILE="$GENESIS_ROOT/data/genesis.db"
if [ -f "$DB_FILE" ]; then
    echo "--- Snapshotting database ---"
    sqlite3 "$DB_FILE" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
    cp "$DB_FILE" "$DB_FILE.pre-update" 2>/dev/null && \
        echo "  DB snapshot: $DB_FILE.pre-update" || \
        echo "  WARNING: DB snapshot failed (continuing anyway)"
fi

# ── Stop services for update ──────────────────────────────
echo "--- Stopping services for update ---"
WERE_RUNNING=()

# Check genesis-server
if systemctl --user is-active --quiet genesis-server.service 2>/dev/null || \
   pgrep -f "python -m genesis serve" >/dev/null 2>&1; then
    _stop_genesis_server
    # Disarm systemd's on-failure auto-restart so a stale-code instance can't come
    # back during the merge/migration window. The ERR-trap rollback is not armed
    # yet, so aborting here leaves the repo/DB untouched — safe to exit.
    if ! _ensure_server_down; then
        echo "  Aborting update — could not stop genesis-server; refusing to merge over a live process."
        echo "  genesis-server has been stopped and was NOT restarted. Bring it back with:"
        echo "    systemctl --user restart genesis-server.service"
        exit 1
    fi
    WERE_RUNNING+=("genesis-server")
fi

# Check genesis-bridge
for svc in genesis-bridge; do
    if systemctl --user is-active --quiet "$svc.service" 2>/dev/null; then
        systemctl --user stop "$svc.service" || true
        WERE_RUNNING+=("$svc")
    fi
done

[[ ${#WERE_RUNNING[@]} -gt 0 ]] && echo "  Stopped: ${WERE_RUNNING[*]}" || echo "  No services were running"
echo ""

# ── update_history helper ────────────────────────────────
# Records an entry in update_history. Silently no-ops if the table
# doesn't exist yet (first update before migration 0001 has run).
_record_update_history() {
    local status="$1"           # success | failed | rolled_back | conflicts_pending
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

    # Stop any running services first. _ensure_server_down also disarms the
    # on-failure restart timer so a stale instance can't come back mid-rollback
    # (best-effort here — the rollback continues even if it can't fully stop).
    _stop_genesis_server
    _ensure_server_down || echo "  WARNING: genesis-server may still be running during rollback"
    systemctl --user stop genesis-bridge 2>/dev/null || true

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
        if [ "$svc" = "genesis-server" ]; then
            _start_genesis_server || echo "  CRITICAL: failed to restart genesis-server"
        else
            systemctl --user start "$svc.service" 2>/dev/null || \
                echo "  CRITICAL: failed to restart $svc"
        fi
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

    # Write failure context for CC to pick up.
    # Values passed as positional args so json.dump() handles all escaping —
    # raw git output in $reason can contain quotes and backslashes.
    mkdir -p "$HOME/.genesis"
    python3 -c "
import json, sys
data = {
    'old_tag':             sys.argv[1],
    'new_tag':             sys.argv[2],
    'old_commit':          sys.argv[3],
    'new_commit':          sys.argv[4],
    'rollback_tag':        sys.argv[5],
    'reason':              sys.argv[6],
    'degraded_subsystems': sys.argv[7],
    'original_branch':     sys.argv[8],
    'rollback_complete':   sys.argv[9] == 'true',
    'timestamp':           sys.argv[10],
}
with open(sys.argv[11], 'w') as f:
    json.dump(data, f, indent=4)
" "$OLD_TAG" "$NEW_TAG" "$OLD_COMMIT" "$NEW_COMMIT" "$ROLLBACK_TAG" \
  "$reason" "${degraded:-}" "$ORIGINAL_BRANCH" \
  "$([ "$checkout_ok" = "true" ] && [ "$pip_ok" = "true" ] && echo true || echo false)" \
  "$(date -Iseconds)" \
  "$HOME/.genesis/last_update_failure.json"

    # Clear the in-progress signal files (mirrors the success-path cleanup) so a
    # leftover entry can't suppress the watchdog's deploy-restart guard after a
    # rollback. The server is back up (above); once this invocation exits its PID
    # dies anyway, but removing the files closes the PID-reuse window proactively.
    rm -f "$STATE_FILE" "$HOME/.genesis/update_in_progress.pid"

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

if [[ "$POST_MERGE" == "false" ]]; then
_write_state "merging"

# ── Fetch + Merge ────────────────────────────────────────
echo "--- Fetching latest ---"
git -C "$GENESIS_ROOT" fetch "$UPDATE_REMOTE" main

# Clear local edits to known-ephemeral tracked files (EPHEMERAL_DIRTY_RE) before
# merging. They are rewritten in place at runtime and regenerate themselves
# (bootstrap seed / promoter / GitNexus), so an incoming change to one of them
# (e.g. this release de-tracking config/procedure_triggers.yaml) must not abort
# the merge. The pre-merge dirty guard EXCUSES these paths; clearing them here is
# the matching half so the merge actually applies.
# Transitional (settings.local.json de-track): if the file is still TRACKED
# (pre-de-track install), back up the live copy OUTSIDE the repo, then clear
# local edits so the upstream deletion merges clean. Restored (untracked)
# right after the merge join point below. Steady state (already untracked):
# ls-files fails -> no-op.
# BEGIN settings-local-premerge (extracted by tests/test_scripts/test_update_settings_local_transition.py)
SETTINGS_LOCAL=".claude/settings.local.json"
SETTINGS_LOCAL_BAK="$HOME/.genesis/settings.local.json.premerge"
if git -C "$GENESIS_ROOT" ls-files --error-unmatch "$SETTINGS_LOCAL" &>/dev/null \
   && [ -f "$GENESIS_ROOT/$SETTINGS_LOCAL" ]; then
    mkdir -p "$HOME/.genesis"
    cp "$GENESIS_ROOT/$SETTINGS_LOCAL" "$SETTINGS_LOCAL_BAK"
    git -C "$GENESIS_ROOT" checkout HEAD -- "$SETTINGS_LOCAL" 2>/dev/null \
        && echo "  (backed up live $SETTINGS_LOCAL; cleared local edits pre-merge)"
fi
# END settings-local-premerge

for _eph in AGENTS.md config/procedure_triggers.yaml; do
    if git -C "$GENESIS_ROOT" ls-files --error-unmatch "$_eph" &>/dev/null \
       && ! git -C "$GENESIS_ROOT" diff --quiet HEAD -- "$_eph" 2>/dev/null; then
        # `checkout HEAD --` (not `checkout --`) restores BOTH index and worktree
        # from HEAD, so a staged edit is cleared too — `checkout --` alone would
        # leave a staged change and the merge would still abort.
        git -C "$GENESIS_ROOT" checkout HEAD -- "$_eph" 2>/dev/null \
            && echo "  (discarded local edits to ephemeral $_eph before merge)"
    fi
done

echo "--- Merging $UPDATE_REMOTE/main ---"
MERGE_OUTPUT=""
MERGE_RC=0
MERGE_OUTPUT=$(git -C "$GENESIS_ROOT" merge "$UPDATE_REMOTE/main" --no-edit 2>&1) || MERGE_RC=$?

if [[ $MERGE_RC -ne 0 ]]; then
    # Check if this is a merge conflict (unmerged paths) vs other error
    if git -C "$GENESIS_ROOT" diff --name-only --diff-filter=U 2>/dev/null | grep -q .; then
        CONFLICTED_FILES=$(git -C "$GENESIS_ROOT" diff --name-only --diff-filter=U)
        echo "  Merge conflicts detected in:"
        echo "$CONFLICTED_FILES" | sed 's/^/    /'

        # Write conflict context for supervising CC session
        mkdir -p "$HOME/.genesis"
        # Build JSON array of conflicted files
        CONFLICT_JSON=$(echo "$CONFLICTED_FILES" | awk '{printf "\"%s\",", $0}' | sed 's/,$//')
        cat > "$HOME/.genesis/update_conflicts.json" << CEOF
{
    "old_tag": "$OLD_TAG",
    "old_commit": "$OLD_COMMIT",
    "target_tag": "$(git -C "$GENESIS_ROOT" describe --tags --match 'v*' --abbrev=0 "$UPDATE_REMOTE/main" 2>/dev/null || echo 'untagged')",
    "target_commit": "$(git -C "$GENESIS_ROOT" rev-parse --short "$UPDATE_REMOTE/main" 2>/dev/null || echo 'unknown')",
    "conflicted_files": [$CONFLICT_JSON],
    "merge_output": "$(echo "$MERGE_OUTPUT" | head -20 | sed 's/"/\\"/g')",
    "timestamp": "$(date -Iseconds)"
}
CEOF
        echo ""
        echo "  Conflict context written to ~/.genesis/update_conflicts.json"

        # Abort the merge — don't leave the working tree in a broken state.
        # CC will resolve conflicts in a worktree, not in the main checkout.
        echo "  Aborting merge to keep working tree clean..."
        git -C "$GENESIS_ROOT" merge --abort 2>/dev/null || true

        # Restart services with original code so the system stays operational
        echo "  Restarting services with pre-update code..."
        for svc in "${WERE_RUNNING[@]}"; do
            if [ "$svc" = "genesis-server" ]; then
                _start_genesis_server || echo "  WARNING: failed to restart genesis-server"
            else
                systemctl --user start "$svc.service" 2>/dev/null || \
                    echo "  WARNING: failed to restart $svc"
            fi
        done

        echo "  System is running on pre-update code."
        echo "  A CC session will resolve conflicts in a worktree."
        _record_update_history "conflicts_pending" \
            "merge conflicts in: $(echo "$CONFLICTED_FILES" | tr '\n' ', ' | sed 's/, $//')" ""
        trap - ERR
        exit 2
    else
        # Not a conflict — some other merge error. Call rollback directly so the
        # DB gets a meaningful reason instead of "command failed (exit 1): false".
        echo "  Merge failed: $MERGE_OUTPUT"
        trap - ERR
        _do_rollback "git merge failed: $(echo "$MERGE_OUTPUT" | head -3 | tr '\n' ' ')"
        exit 1
    fi
fi

NEW_TAG=$(git -C "$GENESIS_ROOT" describe --tags --match 'v*' --abbrev=0 2>/dev/null || echo "untagged")
NEW_COMMIT=$(git -C "$GENESIS_ROOT" rev-parse --short HEAD)

if [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then
    echo "  Already up to date ($NEW_COMMIT)."
    # Clean up unnecessary rollback tag and disarm trap
    trap - ERR
    git -C "$GENESIS_ROOT" tag -d "$ROLLBACK_TAG" 2>/dev/null || true
    # Restart services that we stopped (if any)
    for svc in "${WERE_RUNNING[@]}"; do
        if [ "$svc" = "genesis-server" ]; then
            _start_genesis_server || true
        else
            systemctl --user start "$svc.service" 2>/dev/null || true
        fi
    done
    # Nothing changed and no --post-merge continuation follows, so clear the
    # in-progress signals (like the success path) — a leftover must never linger.
    rm -f "$STATE_FILE" "$HOME/.genesis/update_in_progress.pid"
    # Transitional: the pre-merge step may have cleared live settings.local.json
    # edits — put them back even though no merge landed.
    if [ -f "$SETTINGS_LOCAL_BAK" ]; then
        mkdir -p "$(dirname "$GENESIS_ROOT/$SETTINGS_LOCAL")"
        cp "$SETTINGS_LOCAL_BAK" "$GENESIS_ROOT/$SETTINGS_LOCAL"
        rm -f "$SETTINGS_LOCAL_BAK"
        echo "  (restored live $SETTINGS_LOCAL from pre-merge backup)"
    fi
    # Even with no repo delta, heal deploy-target drift (host/container CC + Node
    # pins): a pin bump pulled MANUALLY before this run, or an earlier failed
    # sync, must not leave drift in place just because the merge was a no-op.
    _sync_deploy_targets
    echo ""
    echo "  Nothing to do."
    exit 0
fi
fi  # end: [[ "$POST_MERGE" == "false" ]]

# Transitional (settings.local.json de-track), both fresh-merge and --post-merge
# paths join here: if the merge removed the live file (upstream de-track applied
# to a tracked copy), restore the pre-merge backup as an UNTRACKED file. No-op
# once installs are past the transition (no backup written).
# BEGIN settings-local-restore (extracted by tests/test_scripts/test_update_settings_local_transition.py)
SETTINGS_LOCAL="${SETTINGS_LOCAL:-.claude/settings.local.json}"
SETTINGS_LOCAL_BAK="${SETTINGS_LOCAL_BAK:-$HOME/.genesis/settings.local.json.premerge}"
if [ -f "$SETTINGS_LOCAL_BAK" ]; then
    mkdir -p "$(dirname "$GENESIS_ROOT/$SETTINGS_LOCAL")"
    cp "$SETTINGS_LOCAL_BAK" "$GENESIS_ROOT/$SETTINGS_LOCAL"
    rm -f "$SETTINGS_LOCAL_BAK"
    echo "  (restored live $SETTINGS_LOCAL from pre-merge backup)"
fi
# END settings-local-restore

NEW_TAG=$(git -C "$GENESIS_ROOT" describe --tags --match 'v*' --abbrev=0 2>/dev/null || echo "untagged")
NEW_COMMIT=$(git -C "$GENESIS_ROOT" rev-parse --short HEAD)

if [[ "$POST_MERGE" == "true" ]]; then
    echo "--- Post-merge mode: running bootstrap on conflict-resolved code ---"
    echo "  Merged: $OLD_TAG ($OLD_COMMIT) → $NEW_TAG ($NEW_COMMIT)"
fi
echo ""

# ── What changed ──────────────────────────────────────────
echo "--- Changes ---"
git -C "$GENESIS_ROOT" log "${OLD_COMMIT}..HEAD" --oneline --no-merges | head -20 || true
echo ""

_write_state "bootstrap"

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

_write_state "migrations"

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

# ── Refresh Network Identity in user-level CLAUDE.md ────
# Always refresh ~/.claude/CLAUDE.md with current IPs (unconditional — the
# user-level file should always be current). Uses sentinel blocks.
_user_claude="$HOME/.claude/CLAUDE.md"
mkdir -p "$HOME/.claude"
# Seed file if missing (migration from an older install without user-level file)
if [ ! -f "$_user_claude" ]; then
    cat > "$_user_claude" <<'UCLSEED'
# This Genesis Install — User-Level Configuration

Install-specific overlay to the project CLAUDE.md. Populated by
scripts/host-setup.sh and refreshed by scripts/update.sh. The
<!-- begin:SECTION --> / <!-- end:SECTION --> blocks below are
managed by install scripts — edit at your own risk. The "Personal Notes"
section is safe to hand-edit; install scripts preserve it.

<!-- begin:container-specs -->
## Container
- **Specs**: (run host-setup.sh to detect and populate)
<!-- end:container-specs -->

<!-- begin:network-identity -->
<!-- end:network-identity -->

<!-- begin:github-config -->
## GitHub
- **Working Repo**: (set by installer)
- **Backups Repo**: (set by installer)
- **Public Distribution**: (set by installer)
<!-- end:github-config -->

## Personal Notes

(Install scripts preserve this section. Add any machine-specific
reminders here.)
UCLSEED
fi
echo "--- Refreshing Network Identity in ~/.claude/CLAUDE.md ---"
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

sed -i '/<!-- begin:network-identity -->/,/<!-- end:network-identity -->/d' "$_user_claude"
{
    echo "<!-- begin:network-identity -->"
    echo "## Network Identity"
    echo ""
    printf -- "- **Container IP**: %s" "${_c_ip:-localhost}"
    [ -n "$_c_ipv6" ] && printf " (v6: %s)" "$_c_ipv6"
    echo ""
    printf -- "- **Host VM IP**: %s\n" "${_host_ip:-localhost}"
    printf -- "- **Dashboard**: http://%s:5000 (via proxy device)\n" "${_host_ip:-localhost}"
    echo "<!-- end:network-identity -->"
} >> "$_user_claude"
echo "  Network identity updated in ~/.claude/CLAUDE.md"
echo ""

_write_state "health_check"

# ── Restart services ──────────────────────────────────────
if [[ ${#WERE_RUNNING[@]} -gt 0 ]]; then
    echo "--- Restarting services ---"

    # Reload systemd in case templates changed during bootstrap
    systemctl --user daemon-reload 2>/dev/null || true

    for svc in "${WERE_RUNNING[@]}"; do
        if [ "$svc" = "genesis-server" ]; then
            if ! _start_genesis_server; then
                _do_rollback "failed to start genesis-server after update"
                exit 1
            fi
        elif ! systemctl --user start "$svc.service"; then
            _do_rollback "failed to start $svc after update"
            exit 1
        fi
    done

    # ── Health verification with retry (FATAL on failure) ─
    echo "--- Verifying system health ---"
    HEALTH_OK=false
    DEGRADED=""

    for attempt in $(seq 1 12); do
        sleep 15
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
        _do_rollback "health endpoint did not respond after 12 attempts (3 minutes)"
        exit 1
    fi
    echo ""
fi

# ── Success: disarm trap ──────────────────────────────────
trap - ERR

_sync_deploy_targets

# ── Clear update failure file on success ──────────────────
if [ -f "$HOME/.genesis/last_update_failure.json" ]; then
    rm -f "$HOME/.genesis/last_update_failure.json"
    echo "  Cleared previous update failure context"
fi

# ── Record success in update_history ─────────────────────
# Container update succeeded; $HOST_CC_DEGRADED (if set) flags a host-side
# Node/CC alignment gap as a degraded subsystem so it is surfaced, not silent.
if [ -n "$HOST_CC_DEGRADED" ]; then
    echo "  NOTE: recording degraded subsystem: $HOST_CC_DEGRADED"
fi
_record_update_history "success" "" "$HOST_CC_DEGRADED"

_write_state "done"

# Clean up state files — successful update, nothing to recover
rm -f "$STATE_FILE"
rm -f "$HOME/.genesis/update_conflicts.json"
rm -f "$HOME/.genesis/last_update_summary.txt"
# Clean up PID file
rm -f "$HOME/.genesis/update_in_progress.pid"

# ── Done ──────────────────────────────────────────────────
echo "  ──────────────────────────────────────"
echo "  Updated: $OLD_TAG ($OLD_COMMIT) → $NEW_TAG ($NEW_COMMIT)"
echo ""
