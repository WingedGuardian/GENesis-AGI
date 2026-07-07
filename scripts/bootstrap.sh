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

# --- Claude Code version pin (install or align to the pinned version) ---
# Node/npm are set up above. Align the local Claude Code to the repo pin so a
# bump reaches the container via `bootstrap.sh` too (not just install.sh).
# Non-fatal (`|| true`): `set -e` is active — a CC hiccup must not abort bootstrap.
_cc_env="$SCRIPT_DIR/lib/cc_version.sh"
if [ -f "$_cc_env" ]; then
    echo "--- Aligning Claude Code to pinned version ---"
    # shellcheck source=/dev/null
    source "$_cc_env"
    cc_ensure_local || true
    cc_shadow_scan || true
fi

# bubblewrap (sandbox for codex exec — optional)
if ! command -v bwrap &>/dev/null; then
    echo "  bubblewrap not found — installing..."
    install_pkg bubblewrap || echo "  WARNING: Could not install bubblewrap. Codex sandbox will use bypass mode."
fi

# Codex CLI (optional — cross-vendor adversarial verification)
if _node_version_ok; then
    if ! command -v codex &>/dev/null; then
        echo "  Codex CLI not found — installing (optional)..."
        npm install -g @openai/codex 2>/dev/null || echo "  WARNING: Could not install Codex CLI. Adversarial verification will use CC invoker fallback."
    fi
    if command -v codex &>/dev/null; then
        echo "  Codex: $(codex --version 2>/dev/null || echo 'installed')"
        # Create default config if not present
        CODEX_CONFIG_DIR="$HOME/.codex"
        CODEX_CONFIG="$CODEX_CONFIG_DIR/config.toml"
        if [[ ! -f "$CODEX_CONFIG" ]]; then
            mkdir -p "$CODEX_CONFIG_DIR"
            cat > "$CODEX_CONFIG" <<'CODEXCFG'
model = "gpt-5.4"
model_reasoning_effort = "medium"
CODEXCFG
            echo "  Created default Codex config at $CODEX_CONFIG"
        fi
        echo "  NOTE: Run 'codex auth login' to enable cross-vendor adversarial verification"
    fi
fi
echo

# --- Code Intelligence Tools ---
echo "--- Installing code intelligence tools ---"

# codebase-memory-mcp (code graph — 66 languages)
# Always re-runs the upstream installer: it is idempotent and pulls the latest
# release, so existing installs are upgraded in place.
echo "  codebase-memory-mcp: installing/upgrading..."
curl -fsSL https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh | bash -s -- --ui \
    || echo "  WARNING: codebase-memory-mcp install/upgrade failed (non-critical)"
if command -v codebase-memory-mcp &>/dev/null; then
    echo "  codebase-memory-mcp: $(codebase-memory-mcp --version 2>/dev/null || echo 'installed')"
fi

# GitNexus (blast radius, impact analysis, execution flows)
# Exact pin to 1.6.8 — only ship versions we've actually verified. 1.6.8 (stable):
# `analyze` works; FTS degrades gracefully when the extension is absent. The prior
# 1.6.4-rc line crashed `analyze` silently. Re-verify before bumping further.
if _node_version_ok; then
    if ! command -v gitnexus &>/dev/null; then
        echo "  GitNexus not found — installing..."
        npm install -g gitnexus@1.6.8 2>/dev/null || echo "  WARNING: GitNexus install failed (non-critical)"
    else
        echo "  GitNexus: ensuring pin..."
        npm install -g gitnexus@1.6.8 2>/dev/null || echo "  WARNING: GitNexus upgrade failed (non-critical)"
    fi
    if command -v gitnexus &>/dev/null; then
        echo "  GitNexus: $(gitnexus --version 2>/dev/null)"
    fi
fi

# Serena (Python LSP — symbols, references, rename)
if ! command -v uv &>/dev/null; then
    echo "  uv not found — installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null \
        || echo "  WARNING: uv install failed (non-critical)"
    export PATH="$HOME/.local/bin:$PATH"
fi
if command -v uv &>/dev/null; then
    if ! command -v serena &>/dev/null; then
        echo "  Serena not found — installing..."
        uv tool install serena-agent 2>/dev/null || echo "  WARNING: Serena install failed (non-critical)"
    else
        echo "  Serena: upgrading..."
        uv tool upgrade serena-agent 2>/dev/null || echo "  WARNING: Serena upgrade failed (non-critical)"
    fi
fi
if command -v serena &>/dev/null; then
    echo "  Serena: $(serena --version 2>/dev/null || echo 'installed')"
fi

# SkillSpector (NVIDIA — skill-security scanner; external dep, not vendored).
# The scheduled skill-security scan resolves the binary from this stable path.
SKILLSPECTOR_DIR="$HOME/.genesis/deps/skillspector"
if [[ ! -x "$SKILLSPECTOR_DIR/.venv/bin/skillspector" ]]; then
    echo "  SkillSpector not found — installing..."
    mkdir -p "$HOME/.genesis/deps"
    if [[ ! -d "$SKILLSPECTOR_DIR/.git" ]]; then
        git clone --depth 1 https://github.com/NVIDIA/SkillSpector.git "$SKILLSPECTOR_DIR" 2>/dev/null \
            || echo "  WARNING: SkillSpector clone failed (skill-security scan will no-op until installed)"
    fi
    if [[ -d "$SKILLSPECTOR_DIR" ]]; then
        "$PYTHON_BIN" -m venv "$SKILLSPECTOR_DIR/.venv" 2>/dev/null \
            && "$SKILLSPECTOR_DIR/.venv/bin/pip" install -q "$SKILLSPECTOR_DIR" 2>/dev/null \
            || echo "  WARNING: SkillSpector install failed (non-critical)"
    fi
fi
if [[ -x "$SKILLSPECTOR_DIR/.venv/bin/skillspector" ]]; then
    echo "  SkillSpector: installed at $SKILLSPECTOR_DIR/.venv/bin/skillspector"
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

# --- Skill-security trusted baseline ---
# Bless the currently-installed skills so the scheduled scan files findings only
# for NEW/untrusted skills (a raw scan flags ~every capable skill as CRITICAL).
echo "--- Seeding skill-security trusted allowlist ---"
"$VENV_DIR/bin/python" -m genesis.security.skill_scan --seed-trusted 2>&1 | tail -1 || true
echo

# --- Baseline procedures ---
# Seed procedural_memory with battle-tested baseline procedures (CORE/LIBRARY,
# deterministic IDs, idempotent upsert) and regenerate the PreToolUse advisor's
# trigger cache from the DB. Without this the shipped advisor cache references
# procedure IDs that aren't DB rows, so advisory surfacing isn't tracked.
echo "--- Seeding baseline procedures ---"
# Non-fatal, but surface a failure instead of swallowing it (a silent miss would
# leave advisory surfacing untracked with no signal). Capture, then check for the
# success line rather than relying on the exit code through the pipe.
# Defense-in-depth (incident IR-2): bound the seed with `timeout 300`. Seeding
# normally finishes in seconds; if the DB write lock is ever contended (a mid-
# deploy server revival deadlocked it, hanging bootstrap ~30 min silently), fail
# FAST at 5 min — the `|| true` keeps it non-fatal and the WARNING below fires.
SEED_OUT="$(timeout 300 "$VENV_DIR/bin/python" "$GENESIS_ROOT/scripts/seed_procedures.py" 2>&1)" || true
echo "$SEED_OUT" | tail -2
if ! echo "$SEED_OUT" | grep -q "Seeded .* procedures"; then
    echo "  WARNING: baseline procedure seeding did not complete — advisor surfacing tracking may be degraded."
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

# --- Identity seed files (auto-generated at runtime, gitignored) ---
echo "--- Checking identity seed files ---"
for f in TRIAGE_CALIBRATION.md USER_KNOWLEDGE.md; do
    if [[ ! -f "$GENESIS_ROOT/src/genesis/identity/$f" ]]; then
        if [[ -f "$GENESIS_ROOT/src/genesis/identity/$f.example" ]]; then
            cp "$GENESIS_ROOT/src/genesis/identity/$f.example" "$GENESIS_ROOT/src/genesis/identity/$f"
            echo "  Copied $f.example -> $f"
        fi
    else
        echo "  $f exists"
    fi
done
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

# --- MCP Server Registration (Code Intelligence) ---
echo "--- Registering code intelligence MCP servers ---"
# _register_mcp lives in scripts/lib/mcp_register.sh — shared with install.sh
# so fresh installs and updates register (and drift-heal) identically.
# shellcheck source=lib/mcp_register.sh
. "$SCRIPT_DIR/lib/mcp_register.sh"

if command -v gitnexus &>/dev/null; then
    _register_mcp "gitnexus" "user" "gitnexus" "mcp"
fi
if command -v codebase-memory-mcp &>/dev/null; then
    # Registered via the repo launcher (NOT the bare binary): the launcher
    # wraps the server in a systemd scope with MemoryMax to contain upstream's
    # unbounded leak (DeusData/codebase-memory-mcp#581). See
    # .claude/mcp/run-codebase-memory for the full rationale.
    _register_mcp "codebase-memory-mcp" "user" "$GENESIS_ROOT/.claude/mcp/run-codebase-memory"
fi
if command -v serena &>/dev/null; then
    _register_mcp "serena" "project" "serena" "start-mcp-server" "--context" "claude-code" "--project" "$GENESIS_ROOT"
fi
echo

# --- Code Intelligence Indexing ---
# No direct indexer spawns here: setup_claude_config.py (invoked earlier)
# already triggers indexing through scripts/lib/code_intel_index.sh — the
# single locked + resource-capped entrypoint. The raw spawns this block used
# to hold ran a SECOND concurrent index of the same repo on every bootstrap
# (and a third via the post-commit hook), which once wedged the container in
# a D-state I/O storm. All indexing must go through the entrypoint.
echo "--- Code intelligence indexing: handled by setup_claude_config (locked entrypoint) ---"
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

# CC temp directory (keeps /tmp clean — CC uses TMPDIR)
CC_TMP_DIR="$HOME/.genesis/cc-tmp"
mkdir -p "$CC_TMP_DIR"
chmod 700 "$CC_TMP_DIR"

# Watchgod config — 500MB budget, 150MB sacred ground
mkdir -p "$HOME/.genesis/config"
cat > "$HOME/.genesis/config/watchgod.conf" <<WEOF
CC_TMP_DIR=$CC_TMP_DIR
CC_TMP_BUDGET_MB=500
SACRED_GROUND_MB=150
WEOF
echo "  CC temp: ${CC_TMP_DIR} (budget: 500MB, sacred: 150MB)"

echo "  ~/.genesis/ initialized"
echo

# --- Journal size cap ---
echo "--- Capping systemd journal size ---"
JOURNALD_DROP_IN="/etc/systemd/journald.conf.d/genesis-size-cap.conf"
if [[ "$(sudo cat "$JOURNALD_DROP_IN" 2>/dev/null)" != $'[Journal]\nSystemMaxUse=200M' ]]; then
    sudo mkdir -p /etc/systemd/journald.conf.d/
    printf '[Journal]\nSystemMaxUse=200M\n' | sudo tee "$JOURNALD_DROP_IN" >/dev/null
    sudo systemctl restart systemd-journald 2>/dev/null || true
    echo "  Journal capped at 200MB (~7 days rolling)"
else
    echo "  Journal cap already set (200MB)"
fi
echo

# --- Systemd service sync ---
echo "--- Syncing systemd service files ---"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
SYSTEMD_TEMPLATE_DIR="$GENESIS_ROOT/scripts/systemd"
SERVICES_UPDATED=0

if [[ -d "$SYSTEMD_TEMPLATE_DIR" ]]; then
    mkdir -p "$SYSTEMD_USER_DIR"
    # Detect Claude Code binary directory for systemd PATH injection
    CC_BIN_DIR="$(dirname "$(command -v claude 2>/dev/null)" 2>/dev/null || echo "$HOME/.npm-global/bin")"

    for template in "$SYSTEMD_TEMPLATE_DIR"/*.service.template "$SYSTEMD_TEMPLATE_DIR"/*.timer.template; do
        [[ -f "$template" ]] || continue
        svc_name=$(basename "$template" .template)

        target="$SYSTEMD_USER_DIR/$svc_name"
        rendered=$(sed -e "s|__HOME__|$HOME|g" \
                       -e "s|__VENV__|$GENESIS_ROOT/.venv|g" \
                       -e "s|__REPO_DIR__|$GENESIS_ROOT|g" \
                       -e "s|__CC_BIN_DIR__|$CC_BIN_DIR|g" \
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

    # Enable + start every rendered timer (idempotent), EXCEPT timers that are a
    # deliberate setup step. Without this a fresh install/repair leaves the
    # housekeeping timers (watchdog, disk-hygiene) rendered but dead. The backup
    # timer is intentionally skipped: it needs a passphrase + a verify run before
    # it should fire (see the "Backups are NOT auto-enabled" note below and
    # SETUP.md "Backups") — auto-enabling a 6h schedule here gives a false sense
    # of safety while data is still local-only.
    for template in "$SYSTEMD_TEMPLATE_DIR"/*.timer.template; do
        [[ -f "$template" ]] || continue
        timer_name=$(basename "$template" .template)
        case "$timer_name" in
            genesis-backup.timer) continue ;;  # deliberate setup step — see note below
        esac
        if [ -f "$SYSTEMD_USER_DIR/$timer_name" ]; then
            systemctl --user enable --now "$timer_name" 2>/dev/null && \
                echo "  + $timer_name enabled + started" || true
        fi
    done
else
    echo "  Template directory $SYSTEMD_TEMPLATE_DIR not found — skipping"
fi

# tmp-watchgod is now a template in scripts/systemd/ — handled by the loop above.

# Check smbclient for Tier 2 backup (optional — only if NAS configured in secrets.env)
if [ -f "$GENESIS_ROOT/secrets.env" ]; then
    if grep -q 'GENESIS_BACKUP_NAS=' "$GENESIS_ROOT/secrets.env" 2>/dev/null; then
        if ! command -v smbclient >/dev/null 2>&1; then
            echo "  WARNING: smbclient not installed — Tier 2 backup to NAS won't work"
            echo "  Install with: sudo apt-get install -y smbclient"
        fi
    fi
fi

# NOTE: Backups are NOT auto-enabled here. The genesis-backup.{service,timer}
# unit FILES are installed by the template-sync loop above, but the timer is
# left disabled: backup is a deliberate setup step (both tiers + passphrase +
# a verify run — see SETUP.md "Backups") handled in the onboarding skill.
# Auto-enabling a 6h schedule on bootstrap just because GENESIS_BACKUP_REPO is
# set gives a false sense of safety (large data is local-only until Tier 2/NAS
# is configured) and isn't a switch to flip for every new user. Enable with:
#   systemctl --user enable --now genesis-backup.timer

# --- VNC stack (collaborative browser mode) ---
echo "--- Setting up VNC stack ---"
if bash "$GENESIS_ROOT/scripts/setup-vnc.sh" 2>&1 | sed 's/^/  /'; then
    echo "  VNC stack ready"
else
    echo "  VNC setup failed (non-fatal — run scripts/setup-vnc.sh manually)"
fi
echo

# --- Memory restore ---
BACKUP_DIR="$GENESIS_ROOT/data/cc-memory-backup"
if [[ -d "$BACKUP_DIR" ]]; then
    echo "--- Restoring CC memory ---"
    bash "$GENESIS_ROOT/scripts/restore_cc_memory.sh" "$GENESIS_ROOT"
    echo
fi

# --- User-level CLAUDE.md seed ---
# CC auto-discovers ~/.claude/CLAUDE.md as user-level instructions alongside
# the project-level file. Install scripts (host-setup.sh, update.sh) manage
# the sentinel blocks; this seed provides the initial structure.
if [ ! -f "$HOME/.claude/CLAUDE.md" ]; then
    mkdir -p "$HOME/.claude"
    cat > "$HOME/.claude/CLAUDE.md" <<'UCLSEED'
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
## Network Identity
- (run host-setup.sh or update.sh to populate)
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
    echo "--- Seeded ~/.claude/CLAUDE.md (run host-setup.sh to populate) ---"
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
