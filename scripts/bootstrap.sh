#!/bin/bash
# Genesis full machine bootstrap.
# Run once after cloning on a new machine.
#
# Usage: ./scripts/bootstrap.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENESIS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== Genesis Bootstrap ==="
echo "Genesis root: $GENESIS_ROOT"
echo

# --- Prerequisites ---
echo "--- Checking prerequisites ---"

if ! command -v python3 &>/dev/null; then
    echo "ERROR: Python 3 not found. Install Python 3.12+."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "  Python: $PYTHON_VERSION"

if ! command -v node &>/dev/null; then
    echo "WARNING: Node.js not found. Some features may not work."
else
    echo "  Node: $(node --version)"
fi

if ! command -v git &>/dev/null; then
    echo "ERROR: git not found."
    exit 1
fi
echo "  git: $(git --version | head -1)"
echo

# --- Python venv ---
echo "--- Setting up Python venv ---"
VENV_DIR="$GENESIS_ROOT/.venv"
if [[ -d "$VENV_DIR" ]]; then
    echo "  Venv already exists at $VENV_DIR"
else
    echo "  Creating venv..."
    python3 -m venv "$VENV_DIR"
    echo "  Installing genesis..."
    "$VENV_DIR/bin/pip" install -e "$GENESIS_ROOT" --quiet
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
    echo "  Current timezone: $CURRENT_TZ"
    read -rp "  Enter timezone (e.g. America/New_York) or press Enter to keep [$CURRENT_TZ]: " INPUT_TZ
    GENESIS_TIMEZONE="${INPUT_TZ:-$CURRENT_TZ}"
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
