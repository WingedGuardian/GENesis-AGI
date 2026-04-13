#!/bin/bash
# Genesis full machine bootstrap.
# Run once after cloning on a new machine.
#
# Usage: ./scripts/bootstrap.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENESIS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# HOME may be unset in some container environments; derive from passwd
if [[ -z "${HOME:-}" ]]; then
    HOME="$(getent passwd "$(whoami)" | cut -d: -f6)"
    export HOME
fi

echo "=== Genesis Bootstrap ==="
echo "Genesis root: $GENESIS_ROOT"
echo

# ── Crash recovery: check for interrupted update ─────────
UPDATE_STATE="$HOME/.genesis/update_state.json"
if [ -f "$UPDATE_STATE" ]; then
    echo "--- Detected interrupted update state file ---"
    # Read phase and PID from state file
    STATE_PHASE=$(python3 -c "import json,sys; print(json.load(open('$UPDATE_STATE')).get('phase','unknown'))" 2>/dev/null || echo "unknown")
    STATE_PID=$(python3 -c "import json,sys; print(json.load(open('$UPDATE_STATE')).get('pid',0))" 2>/dev/null || echo "0")

    # Check if the update process is still alive
    if [ "$STATE_PID" -gt 1 ] 2>/dev/null && kill -0 "$STATE_PID" 2>/dev/null; then
        echo "  Update process (pid $STATE_PID) still running in phase '$STATE_PHASE' — not interfering."
    elif [ "$STATE_PHASE" = "done" ]; then
        echo "  Update completed successfully — cleaning up state file."
        rm -f "$UPDATE_STATE"
    else
        echo "  Update CRASHED in phase '$STATE_PHASE' (pid $STATE_PID is dead)."

        # Abort any in-progress merge
        if [ -f "$GENESIS_ROOT/.git/MERGE_HEAD" ]; then
            echo "  Aborting in-progress merge..."
            git -C "$GENESIS_ROOT" merge --abort 2>/dev/null || true
        fi

        # Read rollback tag from state file
        ROLLBACK_TAG=$(python3 -c "import json,sys; print(json.load(open('$UPDATE_STATE')).get('rollback_tag',''))" 2>/dev/null || echo "")

        if [ -n "$ROLLBACK_TAG" ] && git -C "$GENESIS_ROOT" rev-parse "$ROLLBACK_TAG" >/dev/null 2>&1; then
            echo "  Rolling back to $ROLLBACK_TAG..."
            git -C "$GENESIS_ROOT" reset --hard "$ROLLBACK_TAG" 2>&1 || true
            echo "  Rollback complete."
        else
            echo "  No rollback tag found — resetting to HEAD."
            git -C "$GENESIS_ROOT" reset --hard HEAD 2>&1 || true
        fi

        # Record crash recovery
        echo "  Recording crash recovery in update_history..."
        DB_PATH="$GENESIS_ROOT/data/genesis.db"
        if [ -f "$DB_PATH" ]; then
            python3 -c "
import sqlite3, uuid, json
from datetime import datetime, timezone
state = json.load(open('$UPDATE_STATE'))
try:
    con = sqlite3.connect('$DB_PATH', timeout=5)
    con.execute(
        'INSERT INTO update_history (id, old_tag, new_tag, old_commit, new_commit, status, '
        'rollback_tag, failure_reason, started_at, completed_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (str(uuid.uuid4()), state.get('old_tag',''), '', state.get('old_commit',''), '',
         'crashed_recovered', state.get('rollback_tag',''),
         f\"Crashed in phase: {state.get('phase','unknown')}\",
         state.get('started_at',''), datetime.now(timezone.utc).isoformat()))
    con.commit()
    con.close()
except Exception as e:
    print(f'  WARNING: failed to record crash recovery: {e}')
" 2>/dev/null || echo "  WARNING: could not record crash in update_history"
        fi

        rm -f "$UPDATE_STATE"
        rm -f "$HOME/.genesis/update_in_progress.pid"
        echo "  Crash recovery complete. Continuing bootstrap with rolled-back code."
        echo ""
    fi
fi

# --- Prerequisites ---
echo "--- Checking and installing prerequisites ---"

# Detect package manager
if command -v apt-get &>/dev/null; then
    PKG_MGR="apt"
elif command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
elif command -v yum &>/dev/null; then
    PKG_MGR="yum"
else
    PKG_MGR=""
fi

install_pkg() {
    local pkg_apt="$1"
    local pkg_dnf="${2:-$1}"
    local output rc
    if [[ -z "$PKG_MGR" ]]; then
        echo "  ERROR: No package manager found. Install $pkg_apt manually."
        return 1
    fi
    if [[ "$PKG_MGR" == "apt" ]]; then
        output=$(sudo apt-get install -y -qq "$pkg_apt" 2>&1)
    else
        output=$(sudo "$PKG_MGR" install -y -q "$pkg_dnf" 2>&1)
    fi
    rc=$?
    if [[ $rc -ne 0 ]]; then
        # Show the last meaningful line of output for diagnostics
        local last_line
        last_line=$(echo "$output" | grep -v '^\s*$' | tail -1)
        echo "  install failed (exit $rc): ${last_line:-no output}"
    fi
    return $rc
}

# TMPDIR management — /tmp may be a small tmpfs (512MB on containers).
# pip needs space for large wheels; redirect to ~/tmp/ if /tmp is tight.
_tmp_avail=$(df -BM /tmp 2>/dev/null | awk 'NR==2{gsub(/M/,"",$4); print $4}' || echo "0")
if [[ "${_tmp_avail:-0}" -lt 2048 ]]; then
    mkdir -p "$HOME/tmp"
    export TMPDIR="$HOME/tmp"
    echo "  /tmp is small (${_tmp_avail}MB free) — using $TMPDIR for pip downloads"
fi

# Update package index once before any installs
echo "  Updating package index..."
if [[ "$PKG_MGR" == "apt" ]]; then
    if ! timeout 120 sudo apt-get update -qq 2>/dev/null; then
        echo "  WARNING: Package index update failed — installs may use stale index"
    fi
elif [[ "$PKG_MGR" == "dnf" || "$PKG_MGR" == "yum" ]]; then
    # check-update exits 100 when updates are available — not an error
    sudo "$PKG_MGR" check-update -q 2>/dev/null; true
fi

# Python 3.12+ required
if command -v python3.12 &>/dev/null; then
    echo "  Python: $(python3.12 --version)"
elif command -v python3 &>/dev/null && python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>/dev/null; then
    echo "  Python: $(python3 --version)"
else
    _found_ver=$(python3 --version 2>/dev/null || echo "not found")
    echo "  Python 3.12+ required (found: $_found_ver) — run install.sh to install it"
    echo "  Or install manually and re-run bootstrap."
    exit 1
fi
PYTHON_BIN=$(command -v python3.12 || command -v python3)
PYTHON_VERSION=$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')

# python3-venv (needed for venv creation on Debian/Ubuntu)
if [[ "$PKG_MGR" == "apt" ]]; then
    if ! "$PYTHON_BIN" -c "import ensurepip" &>/dev/null; then
        echo "  python3.${PYTHON_VERSION#*.}-venv not found — installing..."
        install_pkg "python${PYTHON_VERSION}-venv" || install_pkg python3-venv || {
            echo "ERROR: Could not install python3-venv."
            exit 1
        }
    fi
fi

# pip (may be missing on minimal installs)
if ! "$PYTHON_BIN" -m pip --version &>/dev/null; then
    echo "  pip not found — installing..."
    install_pkg python3-pip || { echo "WARNING: Could not install pip. Venv install may fail."; }
fi

# git
if ! command -v git &>/dev/null; then
    echo "  git not found — installing..."
    install_pkg git || { echo "ERROR: Could not install git."; exit 1; }
fi
echo "  git: $(git --version | head -1)"

# curl (health checks, backups, API calls)
if ! command -v curl &>/dev/null; then
    echo "  curl not found — installing..."
    install_pkg curl || { echo "ERROR: Could not install curl."; exit 1; }
fi

# jq (safety hook JSON parsing — required for PreToolUse hooks)
if ! command -v jq &>/dev/null; then
    echo "  jq not found — installing..."
    install_pkg jq || { echo "ERROR: Could not install jq."; exit 1; }
fi

# sqlite3 CLI (DB dumps in backup.sh, ad-hoc debugging)
# Package name: "sqlite3" on apt, "sqlite" on dnf/yum
if ! command -v sqlite3 &>/dev/null; then
    echo "  sqlite3 not found — installing..."
    install_pkg sqlite3 sqlite || echo "  WARNING: Could not install sqlite3. Ad-hoc DB queries will require Python."
fi

# gh (GitHub CLI — backup push, recon gatherer, release workflow, onboarding)
if ! command -v gh &>/dev/null; then
    echo "  gh not found — installing..."
    if ! install_pkg gh; then
        echo "  WARNING: gh unavailable — backup push, release workflow, and GitHub recon will not work."
        echo "           Install manually: https://github.com/cli/cli/blob/trunk/docs/install_linux.md"
    fi
fi

# ripgrep (portability checks, code search)
if ! command -v rg &>/dev/null; then
    echo "  ripgrep not found — installing..."
    install_pkg ripgrep || echo "  WARNING: Could not install ripgrep."
fi

# rclone (inbox sync via Dropbox)
if ! command -v rclone &>/dev/null; then
    echo "  rclone not found — installing..."
    install_pkg rclone || echo "  WARNING: Could not install rclone. Inbox sync will be unavailable."
fi

# ffmpeg (video processing skill)
if ! command -v ffmpeg &>/dev/null; then
    echo "  ffmpeg not found — installing..."
    install_pkg ffmpeg || echo "  WARNING: Could not install ffmpeg. Video processing will be unavailable."
fi

# Utility tools
for tool in unzip htop tmux tree; do
    if ! command -v "$tool" &>/dev/null; then
        echo "  $tool not found — installing..."
        install_pkg "$tool" || echo "  WARNING: Could not install $tool."
    fi
done

# Git identity (required for commits on fresh installs)
if ! git -C "$GENESIS_ROOT" config user.name &>/dev/null; then
    git -C "$GENESIS_ROOT" config user.name "Genesis"
    git -C "$GENESIS_ROOT" config user.email "genesis@$(hostname)"
    echo "  Git identity set to Genesis <genesis@$(hostname)>"
else
    echo "  Git identity: $(git -C "$GENESIS_ROOT" config user.name) <$(git -C "$GENESIS_ROOT" config user.email)>"
fi

# Node.js >= 20 (required for Claude Code)
_node_version_ok() {
    command -v node &>/dev/null || return 1
    local ver
    ver=$(node --version 2>/dev/null | sed 's/^v//')
    local major="${ver%%.*}"
    [[ "$major" -ge 20 ]] 2>/dev/null
}
if ! _node_version_ok; then
    if command -v node &>/dev/null; then
        echo "  Node.js $(node --version) is too old (need >= 20) — upgrading..."
    else
        echo "  Node.js not found — installing..."
    fi
    install_pkg nodejs || true
    if ! _node_version_ok; then
        echo "  WARNING: Node.js >= 20 not available. Claude Code will not work."
        echo "           Install via: curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -"
        echo "                        sudo apt-get install -y nodejs"
    fi
fi
if _node_version_ok; then
    echo "  Node: $(node --version)"
else
    echo "  Node: $(node --version 2>/dev/null || echo 'not available') (needs >= 20)"
fi
echo

# --- Python venv ---
echo "--- Setting up Python venv ---"
VENV_DIR="$GENESIS_ROOT/.venv"
if [[ ! -d "$VENV_DIR" ]] || [[ ! -x "$VENV_DIR/bin/python" ]] || [[ ! -x "$VENV_DIR/bin/pip" ]]; then
    [[ -d "$VENV_DIR" ]] && { echo "  Existing venv is broken — recreating..."; rm -rf "$VENV_DIR"; }
    echo "  Creating venv (using $PYTHON_BIN)..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
echo "  Syncing dependencies..."
"$VENV_DIR/bin/pip" install -e "$GENESIS_ROOT" --quiet 2>&1 | tail -1 || true
if ! "$VENV_DIR/bin/python" -c "from genesis.runtime import GenesisRuntime" 2>/dev/null; then
    echo "  FAIL: pip install completed but Genesis is not importable."
    echo "  Re-run: $VENV_DIR/bin/pip install -e $GENESIS_ROOT --verbose"
    exit 1
fi
echo

# --- Secrets ---
echo "--- Checking secrets ---"
SECRETS_FILE="$GENESIS_ROOT/secrets.env"
if [[ -f "$SECRETS_FILE" ]]; then
    echo "  secrets.env exists"
else
    if [[ -f "$GENESIS_ROOT/secrets.env.example" ]]; then
        cp "$GENESIS_ROOT/secrets.env.example" "$SECRETS_FILE"
        chmod 600 "$SECRETS_FILE"
        echo "  Copied secrets.env.example -> secrets.env"
        echo "  *** EDIT secrets.env NOW with your API keys ***"
    else
        echo "  WARNING: No secrets.env or secrets.env.example found"
    fi
fi
echo

# --- Claude Code project config ---
echo "--- Setting up Claude Code config ---"
"$VENV_DIR/bin/python" "$GENESIS_ROOT/scripts/setup_claude_config.py"
echo

# --- Launchers ---
echo "--- Verifying launchers ---"
for launcher in "$GENESIS_ROOT/.claude/hooks/genesis-hook" "$GENESIS_ROOT/.claude/mcp/run-mcp-server"; do
    if [[ -f "$launcher" ]]; then
        chmod +x "$launcher"
        echo "  OK: $(basename "$launcher")"
    else
        echo "  WARNING: Launcher not found: $launcher"
    fi
done
echo

# --- Git hooks (worktree safety, push guards) ---
echo "--- Installing git hooks ---"
HOOKS_SRC="$GENESIS_ROOT/scripts/hooks"
# Handle both regular repos (.git/hooks) and worktrees (.git is a file)
GIT_COMMON_DIR=$(cd "$GENESIS_ROOT" && _gcd=$(git rev-parse --git-common-dir 2>/dev/null) && cd "$_gcd" && pwd || echo "")
if [[ -n "$GIT_COMMON_DIR" ]] && [[ -d "$GIT_COMMON_DIR/hooks" ]]; then
    HOOKS_DST="$GIT_COMMON_DIR/hooks"
elif [[ -d "$GENESIS_ROOT/.git/hooks" ]]; then
    HOOKS_DST="$GENESIS_ROOT/.git/hooks"
else
    echo "  WARNING: .git/hooks not found — skipping"
    HOOKS_DST=""
fi
if [[ -n "$HOOKS_DST" ]]; then
    # Phase 6: prefer sync-hooks.sh if available — it handles the
    # full set (pre-commit, pre-push, post-commit) + helper scripts
    # (emit_bugfix_audit.py) + version tracking via
    # .genesis-hook-versions. Legacy loop remains as a fallback for
    # very old installs that don't have sync-hooks.sh yet.
    if [[ -x "$HOOKS_SRC/sync-hooks.sh" ]]; then
        "$HOOKS_SRC/sync-hooks.sh" --quiet || echo "  WARNING: sync-hooks.sh exited non-zero (may be user-modified — leaving alone)"
        echo "  + hooks synced via sync-hooks.sh"
    else
        for hook in pre-commit pre-push; do
            if [[ -f "$HOOKS_SRC/$hook" ]]; then
                cp "$HOOKS_SRC/$hook" "$HOOKS_DST/$hook"
                chmod +x "$HOOKS_DST/$hook"
                echo "  + $hook"
            fi
        done
    fi
fi
echo

# --- Timezone ---
echo "--- Configuring timezone ---"
# Read from secrets.env if set, otherwise prompt
GENESIS_TIMEZONE=""
if [[ -f "$SECRETS_FILE" ]]; then
    # Check both USER_TIMEZONE (canonical) and GENESIS_TIMEZONE (alias)
    GENESIS_TIMEZONE=$(grep -oP '^USER_TIMEZONE=\K.*' "$SECRETS_FILE" 2>/dev/null || true)
    [[ -z "$GENESIS_TIMEZONE" ]] && GENESIS_TIMEZONE=$(grep -oP '^GENESIS_TIMEZONE=\K.*' "$SECRETS_FILE" 2>/dev/null || true)
fi
if [[ -z "$GENESIS_TIMEZONE" ]]; then
    CURRENT_TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo "UTC")
    if [[ -t 0 ]]; then
        # Interactive — ask the user
        echo "  Current timezone: $CURRENT_TZ"
        read -rp "  Enter timezone (e.g. UTC) or press Enter to keep [$CURRENT_TZ]: " INPUT_TZ
        GENESIS_TIMEZONE="${INPUT_TZ:-$CURRENT_TZ}"
    else
        # Non-interactive — use current or UTC
        GENESIS_TIMEZONE="$CURRENT_TZ"
        echo "  Using timezone: $GENESIS_TIMEZONE (non-interactive)"
    fi
fi
if command -v timedatectl &>/dev/null; then
    sudo timedatectl set-timezone "$GENESIS_TIMEZONE" 2>/dev/null && \
        echo "  System timezone set to $GENESIS_TIMEZONE" || \
        echo "  WARNING: Could not set timezone (sudo required). Set manually: sudo timedatectl set-timezone $GENESIS_TIMEZONE"
else
    echo "  WARNING: timedatectl not found. Set timezone manually."
fi
# Persist to secrets.env for future runs
if [[ -f "$SECRETS_FILE" ]] && ! grep -q "^GENESIS_TIMEZONE=" "$SECRETS_FILE" 2>/dev/null; then
    echo "GENESIS_TIMEZONE=$GENESIS_TIMEZONE" >> "$SECRETS_FILE"
    echo "  Saved to secrets.env"
fi
echo

# --- Runtime state ---
echo "--- Initializing runtime state ---"
mkdir -p "$HOME/.genesis"
touch "$HOME/.genesis/setup-complete"
echo "  ~/.genesis/ initialized"
echo

# --- Systemd service sync ---
echo "--- Syncing systemd service files ---"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
SYSTEMD_TEMPLATE_DIR="$GENESIS_ROOT/scripts/systemd"
SERVICES_UPDATED=0

if [[ -d "$SYSTEMD_TEMPLATE_DIR" ]]; then
    mkdir -p "$SYSTEMD_USER_DIR"
    for template in "$SYSTEMD_TEMPLATE_DIR"/*.service.template "$SYSTEMD_TEMPLATE_DIR"/*.timer.template; do
        [[ -f "$template" ]] || continue
        svc_name=$(basename "$template" .template)

        target="$SYSTEMD_USER_DIR/$svc_name"
        rendered=$(sed -e "s|__HOME__|$HOME|g" \
                       -e "s|__VENV__|$GENESIS_ROOT/.venv|g" \
                       -e "s|__REPO_DIR__|$GENESIS_ROOT|g" \
                       "$template")
        if [[ -f "$target" ]]; then
            current=$(cat "$target")
            if [[ "$rendered" != "$current" ]]; then
                echo "$rendered" > "$target"
                echo "  Updated: $svc_name"
                SERVICES_UPDATED=1
            else
                echo "  OK: $svc_name (unchanged)"
            fi
        else
            echo "$rendered" > "$target"
            echo "  Created: $svc_name"
            SERVICES_UPDATED=1
        fi
    done
    if [[ "$SERVICES_UPDATED" = "1" ]]; then
        systemctl --user daemon-reload 2>/dev/null || true
        echo "  systemd daemon reloaded"
    fi
else
    echo "  Template directory $SYSTEMD_TEMPLATE_DIR not found — skipping"
fi
echo

# --- Memory restore ---
BACKUP_DIR="$GENESIS_ROOT/data/cc-memory-backup"
if [[ -d "$BACKUP_DIR" ]]; then
    echo "--- Restoring CC memory ---"
    bash "$GENESIS_ROOT/scripts/restore_cc_memory.sh" "$GENESIS_ROOT"
    echo
fi

# --- Settings.local ---
LOCAL_SETTINGS="$GENESIS_ROOT/.claude/settings.local.json"
LOCAL_TEMPLATE="$GENESIS_ROOT/config/settings.local.json.template"
if [[ ! -f "$LOCAL_SETTINGS" && -f "$LOCAL_TEMPLATE" ]]; then
    echo "--- Copying settings.local.json template ---"
    cp "$LOCAL_TEMPLATE" "$LOCAL_SETTINGS"
    echo "  Copied template to .claude/settings.local.json"
    echo
fi

# --- Plugin check ---
echo "--- Checking Claude Code plugins ---"
SKILLS_DIR="$HOME/.claude/skills"
PLUGINS_DIR="$HOME/.claude/plugins"

check_plugin() {
    local name="$1"
    if [[ -d "$SKILLS_DIR/$name" ]] || find "$PLUGINS_DIR" -maxdepth 3 -type d -name "$name" 2>/dev/null | grep -q .; then
        return 0
    fi
    return 1
}

MISSING_CRITICAL=""
MISSING_HELPFUL=""

for plugin in superpowers hookify commit-commands; do
    if ! check_plugin "$plugin"; then
        MISSING_CRITICAL="$MISSING_CRITICAL $plugin"
    fi
done

for plugin in code-review feature-dev firecrawl claude-md-management security-guidance claude-code-setup pyright-lsp; do
    if ! check_plugin "$plugin"; then
        MISSING_HELPFUL="$MISSING_HELPFUL $plugin"
    fi
done

if [[ -n "$MISSING_CRITICAL" ]]; then
    echo "  Genesis strongly recommends these plugins:$MISSING_CRITICAL"
    echo "  Install via Claude Code plugin manager."
fi
if [[ -n "$MISSING_HELPFUL" ]]; then
    echo "  These are also helpful to have:$MISSING_HELPFUL"
fi
if [[ -z "$MISSING_CRITICAL" && -z "$MISSING_HELPFUL" ]]; then
    echo "  All recommended plugins installed."
fi
echo

echo "=== Bootstrap complete ==="
echo "Start Claude Code: claude"
