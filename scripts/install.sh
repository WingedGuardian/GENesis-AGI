#!/bin/bash
# Genesis v3 — Full First-Run Installer
# Sets up the complete Genesis environment from a fresh Ubuntu system.
# Handles prerequisites, AZ integration, systemd services, Claude Code,
# interactive API key setup, and smoke testing.
#
# Usage:
#   git clone <genesis-repo>
#   cd genesis
#   ./scripts/install.sh [--non-interactive] [--force-interactive]
#
# Flags:
#   --non-interactive    Skip all prompts (for CI, scripted installs)
#   --force-interactive  Force prompts even if keys already exist
#
# Environment variables (all optional):
#   AZ_ROOT                — Agent Zero checkout (default: ~/agent-zero)
#   VENV_PATH              — Python venv path (default: $REPO_DIR/.venv)
#   SECRETS_PATH           — secrets.env location (default: $REPO_DIR/secrets.env)
#   QDRANT_URL             — Qdrant server URL (default: http://localhost:6333)
#   OLLAMA_URL             — Ollama server URL (default: http://localhost:11434)
#   OLLAMA_EMBEDDING_MODEL — Ollama embedding model (default: qwen3-embedding:0.6b-fp16)
#   GENESIS_ENABLE_OLLAMA  — Enable local Ollama (default: false; cloud is default)
#   QDRANT_VERSION         — Qdrant version to install if missing (default: 1.14.0)

set -euo pipefail

# ── CLI flags ────────────────────────────────────────────────
INTERACTIVE="auto"  # auto | off | on
while [ $# -gt 0 ]; do
    case "$1" in
        --non-interactive)   INTERACTIVE="off" ;;
        --force-interactive) INTERACTIVE="on" ;;
        -h|--help)
            sed -n '2,/^$/{ s/^# \?//; p }' "$0"
            exit 0
            ;;
        *) echo "  Unknown option: $1"; exit 1 ;;
    esac
    shift
done

# Determine whether to prompt the user
should_prompt() {
    [ "$INTERACTIVE" = "on" ] && return 0
    [ "$INTERACTIVE" = "off" ] && return 1
    # auto: prompt only if stdin is a TTY
    [ -t 0 ] && return 0
    return 1
}

# Write a key=value to secrets.env without sed metacharacter risk
set_secret() {
    local key="$1" value="$2" file="$3"
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        grep -v "^${key}=" "$file" > "${file}.tmp" && mv "${file}.tmp" "$file"
    fi
    echo "${key}=${value}" >> "$file"
}

# ── TMPDIR guard ─────────────────────────────────────────────
# pip downloads large wheels (e.g. torch ~2GB) to TMPDIR and will fail
# with "No space left on device" if /tmp is a small tmpfs (512MB in
# Incus/LXC containers).
if [ -z "${TMPDIR:-}" ]; then
    tmp_avail=$(df --output=avail /tmp 2>/dev/null | tail -1 | tr -d ' ')
    if [ -n "$tmp_avail" ] && [ "$tmp_avail" -lt 2097152 ] 2>/dev/null; then
        mkdir -p "$HOME/tmp"
        export TMPDIR="$HOME/tmp"
        echo "  NOTE: /tmp is small ($(df -h /tmp | tail -1 | awk '{print $4}') avail)."
        echo "        Using $TMPDIR for pip downloads to avoid 'No space left on device'."
        echo ""
    fi
fi

# ── Path setup ───────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
AZ_ROOT="${AZ_ROOT:-$HOME/agent-zero}"
VENV_PATH="${VENV_PATH:-$REPO_DIR/.venv}"
SECRETS_FILE="${SECRETS_PATH:-$REPO_DIR/secrets.env}"
SETUP_WARNINGS=0
TOTAL_STEPS=14

echo ""
echo "  Genesis v3 — Setup"
echo "  ─────────────────────────────────────────"
echo ""

# ══════════════════════════════════════════════════════════════
# Pre-flight checks — run BEFORE any mutations. Fail fast.
# ══════════════════════════════════════════════════════════════
echo "  Pre-flight checks..."
PREFLIGHT_OK=1

# Disk: >= 5GB free on $HOME (Genesis ~2GB + Qdrant ~1GB + headroom)
# AZ with torch needs more but torch is optional for cloud-primary setups.
home_avail_kb=$(df --output=avail "$HOME" 2>/dev/null | tail -1 | tr -d ' ')
if [ -n "$home_avail_kb" ] && [ "$home_avail_kb" -lt 5242880 ] 2>/dev/null; then
    home_avail_h=$(df -h "$HOME" | tail -1 | awk '{print $4}')
    echo "    FAIL  Disk: need >= 5GB free on \$HOME, only $home_avail_h available"
    PREFLIGHT_OK=0
elif [ -n "$home_avail_kb" ] && [ "$home_avail_kb" -lt 10485760 ] 2>/dev/null; then
    echo "    WARN  Disk: $(df -h "$HOME" | tail -1 | awk '{print $4}') free (10GB+ recommended)"
else
    echo "    OK    Disk: $(df -h "$HOME" | tail -1 | awk '{print $4}') free"
fi

# RAM
mem_total_kb=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}')
if [ -n "${mem_total_kb:-}" ]; then
    mem_total_gb=$((mem_total_kb / 1048576))
    if [ "$mem_total_kb" -lt 2097152 ]; then
        echo "    FAIL  RAM: ${mem_total_gb}GB (need >= 2GB)"
        PREFLIGHT_OK=0
    elif [ "$mem_total_kb" -lt 4194304 ]; then
        echo "    WARN  RAM: ${mem_total_gb}GB (Genesis will work but may be slow)"
    elif [ "$mem_total_kb" -lt 8388608 ]; then
        echo "    OK    RAM: ${mem_total_gb}GB (8GB+ recommended for torch models)"
    else
        echo "    OK    RAM: ${mem_total_gb}GB"
    fi
fi

# Network: critical (hard fail)
for endpoint in github.com pypi.org; do
    if curl --max-time 5 -sf "https://$endpoint" >/dev/null 2>&1; then
        echo "    OK    Network: $endpoint"
    else
        echo "    FAIL  Network: cannot reach $endpoint"
        PREFLIGHT_OK=0
    fi
done

# Network: cloud providers (warning only)
for endpoint in openrouter.ai api.deepinfra.com; do
    if curl --max-time 5 -sf "https://$endpoint" >/dev/null 2>&1; then
        echo "    OK    Network: $endpoint"
    else
        echo "    WARN  Network: $endpoint unreachable (cloud providers may not work)"
    fi
done

# Ports: check 5000 and 6333
if command -v ss &>/dev/null; then
    for port in 5000 6333; do
        bound_by=$(ss -tlnp "sport = :$port" 2>/dev/null | tail -n +2 | head -1)
        if [ -n "$bound_by" ]; then
            echo "    WARN  Port $port already in use"
        fi
    done
fi

# Python 3.12+
if ! command -v python3 &>/dev/null; then
    echo "    python3 not found — installing..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq python3 > /dev/null 2>&1
    fi
fi
if command -v python3 &>/dev/null; then
    py_version=$(python3 --version 2>&1 | grep -oP '\d+\.\d+' || echo "0.0")
    if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>/dev/null; then
        echo "    OK    Python $py_version"
    else
        echo "    FAIL  Python 3.12+ required, found $py_version"
        PREFLIGHT_OK=0
    fi
else
    echo "    FAIL  python3 not found and could not auto-install"
    PREFLIGHT_OK=0
fi

if [ "$PREFLIGHT_OK" = "0" ]; then
    echo ""
    echo "  Pre-flight FAILED — fix the errors above before continuing."
    exit 1
fi
echo ""


# ══════════════════════════════════════════════════════════════
#  Step 0 — Prerequisites (system deps, AZ, venv, requirements)
# ══════════════════════════════════════════════════════════════
echo "  [0/$TOTAL_STEPS] Installing prerequisites..."

# pip
if ! python3 -m pip --version &>/dev/null; then
    echo "    python3-pip not found — installing..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y -qq python3-pip 2>/dev/null || {
            echo "    ERROR: Could not install python3-pip. Install manually:"
            echo "      sudo apt-get install python3-pip"
            exit 1
        }
        echo "    + python3-pip installed"
    else
        echo "    ERROR: python3-pip not found and apt-get not available."
        exit 1
    fi
fi

# venv module
if ! python3 -c "import venv" &>/dev/null; then
    echo "    python3-venv not found — installing..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y -qq python3-venv 2>/dev/null || {
            echo "    ERROR: Could not install python3-venv."
            exit 1
        }
        echo "    + python3-venv installed"
    else
        echo "    ERROR: python3-venv not found. Install manually."
        exit 1
    fi
fi

# curl
if ! command -v curl &>/dev/null; then
    echo "    curl not found — installing..."
    sudo apt-get install -y -qq curl 2>/dev/null || {
        echo "    ERROR: curl required. Install manually."
        exit 1
    }
    echo "    + curl installed"
fi

# Node.js (optional but recommended)
if ! command -v node &>/dev/null; then
    echo "    Node.js not found — installing..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y -qq nodejs > /dev/null 2>&1 && \
            echo "    + Node.js installed ($(node --version))" || \
            echo "    WARNING: Could not install Node.js. Some features may not work."
    else
        echo "    WARNING: Node.js not found. Install manually for full functionality."
    fi
else
    echo "    . Node.js $(node --version)"
fi

# Agent Zero
if [ ! -d "$AZ_ROOT" ]; then
    echo "    Agent Zero not found at $AZ_ROOT — cloning upstream..."
    git clone https://github.com/frdel/agent-zero.git "$AZ_ROOT" 2>&1 | tail -1
    echo "    + Agent Zero cloned"
fi

# AZ venv
if [ ! -d "$VENV_PATH" ]; then
    echo "    Creating venv at $VENV_PATH..."
    python3 -m venv "$VENV_PATH"
    echo "    + venv created"
fi

# AZ requirements
if ! "$VENV_PATH/bin/python" -c "import flask" &>/dev/null 2>&1; then
    echo "    Installing Agent Zero requirements (this may take several minutes)..."
    "$VENV_PATH/bin/pip" install --upgrade pip --quiet 2>&1 | tail -1 || true
    "$VENV_PATH/bin/pip" install -r "$AZ_ROOT/requirements.txt" --quiet 2>&1 | tail -3 || {
        echo "    WARNING: Some AZ requirements failed to install."
        echo "    Try manually: $VENV_PATH/bin/pip install -r $AZ_ROOT/requirements.txt"
        SETUP_WARNINGS=1
    }
    echo "    + Agent Zero requirements installed"
else
    echo "    . Agent Zero venv OK"
fi

# secrets.env
if [ ! -f "$SECRETS_FILE" ]; then
    if [ -f "$REPO_DIR/secrets.env.example" ]; then
        cp "$REPO_DIR/secrets.env.example" "$SECRETS_FILE"
        chmod 600 "$SECRETS_FILE"
        echo "    + Created secrets.env from template"
    fi
fi

echo ""


# ══════════════════════════════════════════════════════════════
#  Step 1 — Git hooks
# ══════════════════════════════════════════════════════════════
echo "  [1/$TOTAL_STEPS] Installing git hooks..."

HOOKS_SRC="$REPO_DIR/scripts/hooks"
# Handle both regular repos (.git/hooks) and worktrees (.git is a file)
GIT_COMMON_DIR=$(cd "$REPO_DIR" && git rev-parse --git-common-dir 2>/dev/null | xargs realpath 2>/dev/null || echo "")
if [ -n "$GIT_COMMON_DIR" ] && [ -d "$GIT_COMMON_DIR/hooks" ]; then
    HOOKS_DST="$GIT_COMMON_DIR/hooks"
elif [ -d "$REPO_DIR/.git/hooks" ]; then
    HOOKS_DST="$REPO_DIR/.git/hooks"
else
    echo "    WARNING: .git/hooks not found (not a git repo?) — skipping"
    HOOKS_DST=""
fi

if [ -n "$HOOKS_DST" ]; then
    for hook in pre-commit pre-push; do
        if [ -f "$HOOKS_SRC/$hook" ]; then
            cp "$HOOKS_SRC/$hook" "$HOOKS_DST/$hook"
            chmod +x "$HOOKS_DST/$hook"
            echo "    + $hook installed"
        else
            echo "    - $hook source not found (skipping)"
        fi
    done
fi


# ══════════════════════════════════════════════════════════════
#  Step 2 — Directories
# ══════════════════════════════════════════════════════════════
echo "  [2/$TOTAL_STEPS] Creating directories..."

for dir in data logs; do
    if [ ! -d "$REPO_DIR/$dir" ]; then
        mkdir -p "$REPO_DIR/$dir"
        echo "    + $dir/ created"
    else
        echo "    . $dir/ exists"
    fi
done

mkdir -p ~/.genesis 2>/dev/null || true


# ══════════════════════════════════════════════════════════════
#  Step 3 — Secrets configuration
# ══════════════════════════════════════════════════════════════
echo "  [3/$TOTAL_STEPS] Checking secrets configuration..."

if [ -f "$SECRETS_FILE" ]; then
    echo "    . secrets.env found at $SECRETS_FILE"
    perms=$(stat -c %a "$SECRETS_FILE" 2>/dev/null || stat -f %Lp "$SECRETS_FILE" 2>/dev/null || echo "unknown")
    if [ "$perms" != "600" ] && [ "$perms" != "unknown" ]; then
        echo "    WARNING: secrets.env permissions are $perms (should be 600)"
        echo "    Fix with: chmod 600 $SECRETS_FILE"
    fi
else
    echo "    ! secrets.env not found at $SECRETS_FILE"
    OLD_SECRETS="$AZ_ROOT/usr/secrets.env"
    if [ -f "$OLD_SECRETS" ] && [ ! -L "$OLD_SECRETS" ]; then
        echo "    Found existing secrets at old location: $OLD_SECRETS"
        echo "    Migrate with:"
        echo "      mv $OLD_SECRETS $SECRETS_FILE"
        echo "      ln -s $SECRETS_FILE $OLD_SECRETS"
    else
        echo "    Create from template:"
        echo "      cp $REPO_DIR/secrets.env.example $SECRETS_FILE"
        echo "      chmod 600 $SECRETS_FILE"
    fi
fi


# ══════════════════════════════════════════════════════════════
#  Step 4 — Deploy AZ plugins
# ══════════════════════════════════════════════════════════════
echo "  [4/$TOTAL_STEPS] Deploying AZ plugins..."

if [ ! -d "$AZ_ROOT" ]; then
    echo "    ERROR: Agent Zero not found at $AZ_ROOT"
    exit 1
fi

mkdir -p "$AZ_ROOT/usr/plugins"

if [ -d "$REPO_DIR/az_plugins/genesis" ]; then
    cp -r "$REPO_DIR/az_plugins/genesis" "$AZ_ROOT/usr/plugins/"
    echo "    + genesis plugin deployed"
else
    echo "    WARNING: az_plugins/genesis not found in repo"
fi

if [ -d "$REPO_DIR/az_plugins/genesis-memory" ]; then
    cp -r "$REPO_DIR/az_plugins/genesis-memory" "$AZ_ROOT/usr/plugins/"
    echo "    + genesis-memory plugin deployed"
else
    echo "    WARNING: az_plugins/genesis-memory not found in repo"
fi

# Upstream AZ only scans usr/extensions/, not usr/plugins/*/extensions/.
# Symlink each plugin's extension dirs into usr/extensions/ so AZ discovers them.
mkdir -p "$AZ_ROOT/usr/extensions"
for plugin_dir in "$AZ_ROOT"/usr/plugins/*/extensions/*/; do
    ext_type=$(basename "$plugin_dir")
    target="$AZ_ROOT/usr/extensions/$ext_type"
    mkdir -p "$target"
    for ext_file in "$plugin_dir"*.py; do
        [ -f "$ext_file" ] || continue
        fname=$(basename "$ext_file")
        if [ ! -e "$target/$fname" ]; then
            ln -sf "$ext_file" "$target/$fname"
        fi
    done
done
echo "    + plugin extensions symlinked into usr/extensions/"

# Symlink plugin prompts into usr/prompts/
mkdir -p "$AZ_ROOT/usr/prompts"
for plugin_dir in "$AZ_ROOT"/usr/plugins/*/prompts/; do
    [ -d "$plugin_dir" ] || continue
    for prompt_file in "$plugin_dir"*; do
        [ -f "$prompt_file" ] || continue
        fname=$(basename "$prompt_file")
        if [ ! -e "$AZ_ROOT/usr/prompts/$fname" ]; then
            ln -sf "$prompt_file" "$AZ_ROOT/usr/prompts/$fname"
        fi
    done
done
echo "    + plugin prompts symlinked into usr/prompts/"


# ══════════════════════════════════════════════════════════════
#  Step 5 — Patch run_ui.py server_startup hook
# ══════════════════════════════════════════════════════════════
echo "  [5/$TOTAL_STEPS] Checking run_ui.py server_startup hook..."

RUN_UI="$AZ_ROOT/run_ui.py"
if [ ! -f "$RUN_UI" ]; then
    echo "    ERROR: $RUN_UI not found"
    exit 1
fi

if grep -q "server_startup" "$RUN_UI"; then
    echo "    . server_startup hook already present"
else
    echo "    + Patching run_ui.py with Genesis server_startup hook..."
    sed -i '/initialize\.initialize_preload()/a\
\
    # Genesis server startup — initialize background infrastructure\
    # CRITICAL REBASE INVARIANT: Without this, Genesis background systems\
    # (awareness loop, learning scheduler, inbox monitor) never start.\
    # Verify after every AZ update: grep -n "server_startup" run_ui.py\
    from python.helpers import defer, extension\
    defer.DeferredTask("GenesisBootstrap").start_task(\
        extension.call_extensions, "server_startup"\
    ).result_sync()' "$RUN_UI"
    if grep -q "server_startup" "$RUN_UI"; then
        echo "    + server_startup hook patched into run_ui.py"
    else
        echo "    ERROR: sed patch failed — 'initialize_preload()' not found in run_ui.py"
        echo "    Patch manually. See docs/reference/agent-zero-fork-tracking.md"
        SETUP_WARNINGS=1
    fi
fi


# ══════════════════════════════════════════════════════════════
#  Step 6 — Install Genesis package
# ══════════════════════════════════════════════════════════════
echo "  [6/$TOTAL_STEPS] Installing Genesis package..."

if [ -d "$VENV_PATH" ]; then
    # Guard: refuse editable install from inside a worktree.
    # Canonical detection: --git-common-dir differs from --git-dir in worktrees.
    _git_common="$(git rev-parse --git-common-dir 2>/dev/null)"
    _git_dir="$(git rev-parse --git-dir 2>/dev/null)"
    if [ -n "$_git_common" ] && [ -n "$_git_dir" ] && [ "$_git_common" != "$_git_dir" ]; then
        echo "    BLOCKED: pip install -e from a worktree redirects ALL system imports."
        echo "    Use PYTHONPATH=$REPO_DIR/src instead, or run from the main checkout."
    else
        "$VENV_PATH/bin/pip" install -e "$REPO_DIR" --quiet 2>&1 | tail -1 || true
        echo "    + Genesis installed in editable mode"
    fi
else
    echo "    WARNING: venv not found at $VENV_PATH — skipping pip install"
fi


# ══════════════════════════════════════════════════════════════
#  Step 7 — Generate systemd service files from templates
# ══════════════════════════════════════════════════════════════
echo "  [7/$TOTAL_STEPS] Generating systemd service files from templates..."

SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"
SYSTEMD_TEMPLATE_DIR="$REPO_DIR/scripts/systemd"
SERVICES_GENERATED=0

if [ -d "$SYSTEMD_TEMPLATE_DIR" ]; then
    for template in "$SYSTEMD_TEMPLATE_DIR"/*.service.template; do
        [ -f "$template" ] || continue
        svc_name=$(basename "$template" .template)
        target="$SYSTEMD_USER_DIR/$svc_name"
        if [ -f "$target" ]; then
            echo "    . $svc_name already exists (not overwriting)"
        else
            sed -e "s|__HOME__|$HOME|g" \
                -e "s|__VENV__|$VENV_PATH|g" \
                -e "s|__REPO_DIR__|$REPO_DIR|g" \
                -e "s|__AZ_ROOT__|$AZ_ROOT|g" \
                "$template" > "$target"
            echo "    + $svc_name generated"
            SERVICES_GENERATED=1
        fi
    done
else
    echo "    WARNING: Template directory $SYSTEMD_TEMPLATE_DIR not found — skipping"
fi

if [ "$SERVICES_GENERATED" = "1" ]; then
    systemctl --user daemon-reload 2>/dev/null || true
    echo "    + systemd daemon reloaded"
fi


# ══════════════════════════════════════════════════════════════
#  Step 8 — AZ secrets symlink
# ══════════════════════════════════════════════════════════════
echo "  [8/$TOTAL_STEPS] Checking AZ secrets symlink..."

AZ_SECRETS="$AZ_ROOT/usr/secrets.env"
if [ -f "$SECRETS_FILE" ] && [ ! -e "$AZ_SECRETS" ]; then
    ln -s "$SECRETS_FILE" "$AZ_SECRETS"
    echo "    + Symlink: $AZ_SECRETS → $SECRETS_FILE"
elif [ -e "$AZ_SECRETS" ]; then
    echo "    . AZ secrets already exists at $AZ_SECRETS"
else
    echo "    - secrets.env not found — symlink skipped"
fi


# ══════════════════════════════════════════════════════════════
#  Step 9 — Claude Code hooks
# ══════════════════════════════════════════════════════════════
echo "  [9/$TOTAL_STEPS] Setting up Claude Code hooks..."

TEMPLATE="$REPO_DIR/config/claude-settings.json.template"
TARGET="$REPO_DIR/.claude/settings.json"
VENV_PYTHON="$VENV_PATH/bin/python"

if [ -f "$TEMPLATE" ]; then
    mkdir -p "$REPO_DIR/.claude"
    if [ -f "$TARGET" ]; then
        echo "    . .claude/settings.json already exists (not overwriting)"
    else
        sed "s|{{VENV_PYTHON}}|$VENV_PYTHON|g; s|{{GENESIS_ROOT}}|$REPO_DIR|g" \
            "$TEMPLATE" > "$TARGET"
        echo "    + Claude Code hooks configured"
    fi
else
    echo "    - Hook template not found (skipping)"
fi

# .mcp.json — MCP server configuration for Claude Code
MCP_TEMPLATE="$REPO_DIR/config/mcp.json.template"
MCP_TARGET="$REPO_DIR/.mcp.json"
if [ -f "$MCP_TEMPLATE" ]; then
    if [ -f "$MCP_TARGET" ]; then
        echo "    . .mcp.json already exists (not overwriting)"
    else
        sed "s|{{VENV_PYTHON}}|$VENV_PYTHON|g; s|{{GENESIS_ROOT}}|$REPO_DIR|g" \
            "$MCP_TEMPLATE" > "$MCP_TARGET"
        echo "    + MCP server config generated (.mcp.json)"
    fi
else
    echo "    - MCP template not found (skipping)"
fi


# ══════════════════════════════════════════════════════════════
#  Step 10 — Infrastructure services (Qdrant + Ollama)
# ══════════════════════════════════════════════════════════════
echo "  [10/$TOTAL_STEPS] Checking infrastructure..."

# Qdrant (required)
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
QDRANT_VERSION="${QDRANT_VERSION:-1.14.0}"
if curl -sf "$QDRANT_URL/collections" >/dev/null 2>&1; then
    qdrant_ver=$(curl -sf "$QDRANT_URL" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','unknown'))" 2>/dev/null || echo "unknown")
    echo "    . Qdrant reachable at $QDRANT_URL (v${qdrant_ver})"
elif command -v qdrant &>/dev/null; then
    echo "    . Qdrant binary found but not running"
    SETUP_WARNINGS=1
else
    echo "    Qdrant not found — attempting install (v${QDRANT_VERSION})..."
    _qdrant_arch="x86_64"
    [ "$(uname -m)" = "aarch64" ] && _qdrant_arch="aarch64"
    _qdrant_url="https://github.com/qdrant/qdrant/releases/download/v${QDRANT_VERSION}/qdrant-${_qdrant_arch}-unknown-linux-musl.tar.gz"
    if curl -sfL "$_qdrant_url" -o /tmp/qdrant.tar.gz 2>/dev/null; then
        tar xzf /tmp/qdrant.tar.gz -C /tmp/ 2>/dev/null
        if [ -f /tmp/qdrant ]; then
            if sudo mv /tmp/qdrant /usr/local/bin/qdrant 2>/dev/null; then
                echo "    + Qdrant ${QDRANT_VERSION} installed to /usr/local/bin/"
            else
                mkdir -p "$HOME/.local/bin"
                mv /tmp/qdrant "$HOME/.local/bin/qdrant"
                export PATH="$HOME/.local/bin:$PATH"
                echo "    + Qdrant ${QDRANT_VERSION} installed to ~/.local/bin/"
            fi
            rm -f /tmp/qdrant.tar.gz
            # Create data dir and config
            mkdir -p "$HOME/.qdrant/storage"
            if [ ! -f "$HOME/.qdrant/config.yaml" ]; then
                cat > "$HOME/.qdrant/config.yaml" <<QDCONF
storage:
  storage_path: $HOME/.qdrant/storage
service:
  host: 127.0.0.1
  http_port: 6333
  grpc_port: 6334
QDCONF
            fi
        else
            echo "    WARNING: Qdrant download succeeded but binary not found in archive"
        fi
    else
        echo "    WARNING: Could not download Qdrant from $_qdrant_url"
    fi
    echo "    Genesis REQUIRES Qdrant for vector storage."
    SETUP_WARNINGS=1
fi

# Ollama (optional)
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
GENESIS_ENABLE_OLLAMA="${GENESIS_ENABLE_OLLAMA:-false}"
OLLAMA_EMBEDDING_MODEL="${OLLAMA_EMBEDDING_MODEL:-qwen3-embedding:0.6b-fp16}"
if [ "$GENESIS_ENABLE_OLLAMA" = "false" ] || [ "$GENESIS_ENABLE_OLLAMA" = "0" ]; then
    echo "    . Ollama disabled (cloud providers are the default)"
elif curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
    echo "    . Ollama reachable at $OLLAMA_URL"
    echo "    Pulling embedding model: $OLLAMA_EMBEDDING_MODEL"
    ollama pull "$OLLAMA_EMBEDDING_MODEL" 2>/dev/null && \
        echo "    + $OLLAMA_EMBEDDING_MODEL ready" || \
        echo "    WARNING: Failed to pull $OLLAMA_EMBEDDING_MODEL"
else
    echo "    . Ollama not reachable (optional — cloud providers work without it)"
fi


# ══════════════════════════════════════════════════════════════
#  Step 11 — Systemd services + hardening
# ══════════════════════════════════════════════════════════════
echo "  [11/$TOTAL_STEPS] Setting up systemd services..."

# SYSTEMD_USER_DIR already created by Step 7 (template generation)
SERVICES_CREATED=0

# Qdrant user service — skip if a system-level service already exists and runs.
# Qdrant stays inline (not template) because ExecStart depends on the
# dynamically-detected $QDRANT_BIN path from Step 10.
QDRANT_BIN=$(command -v qdrant 2>/dev/null || echo "")
_sys_qdrant_active=$(sudo -n systemctl is-active qdrant 2>/dev/null || echo "inactive")
if [ "$_sys_qdrant_active" = "active" ]; then
    echo "    . Qdrant system service already running — skipping user service"
elif [ -n "$QDRANT_BIN" ] && [ ! -f "$SYSTEMD_USER_DIR/qdrant.service" ]; then
    cat > "$SYSTEMD_USER_DIR/qdrant.service" <<QDSERVICE
[Unit]
Description=Qdrant Vector Database
After=network.target

[Service]
Type=simple
ExecStart=$QDRANT_BIN --config-path $HOME/.qdrant/config.yaml
Restart=always
RestartSec=5
MemoryMax=4G
LimitNOFILE=65536
OOMScoreAdjust=-500
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
QDSERVICE
    echo "    + qdrant.service created"
    SERVICES_CREATED=1
elif [ -f "$SYSTEMD_USER_DIR/qdrant.service" ]; then
    echo "    . qdrant.service already exists"
else
    echo "    - Qdrant binary not found — skipping service"
fi

# agent-zero, genesis-bridge, genesis-watchdog are generated from
# templates in Step 7 — just report their status here.
for _svc in agent-zero genesis-bridge genesis-watchdog; do
    if [ -f "$SYSTEMD_USER_DIR/${_svc}.service" ]; then
        echo "    . ${_svc}.service present"
    else
        echo "    - ${_svc}.service not found (check Step 7 template generation)"
    fi
done

# Enable linger for user services to persist across logout
if command -v loginctl &>/dev/null; then
    if ! loginctl show-user "$(whoami)" 2>/dev/null | grep -q "Linger=yes"; then
        if loginctl enable-linger "$(whoami)" 2>/dev/null; then
            echo "    + linger enabled for $(whoami)"
        else
            echo "    WARNING: could not enable linger (services may stop on logout)"
        fi
    fi
fi

# Reload and enable
if [ "$SERVICES_CREATED" = "1" ] || [ "${SERVICES_GENERATED:-0}" = "1" ]; then
    systemctl --user daemon-reload 2>/dev/null || true

    if [ -f "$SYSTEMD_USER_DIR/qdrant.service" ]; then
        if systemctl --user enable qdrant 2>/dev/null; then
            echo "    + qdrant.service enabled"
        fi

        # Start Qdrant now if it was just installed or isn't running
        if ! curl -sf "$QDRANT_URL/collections" >/dev/null 2>&1; then
            if systemctl --user start qdrant 2>/dev/null; then
                echo "    + qdrant started"
            else
                echo "    WARNING: could not start qdrant"
            fi
            # Wait for Qdrant to initialize (retry up to 10s)
            for _i in 1 2 3 4 5; do
                curl -sf "$QDRANT_URL/collections" >/dev/null 2>&1 && break
                sleep 2
            done
        fi
    fi

    if [ -f "$SYSTEMD_USER_DIR/agent-zero.service" ]; then
        if systemctl --user enable agent-zero 2>/dev/null; then
            echo "    + agent-zero.service enabled"
        fi
        # Do NOT auto-start AZ yet — wait for API keys
    fi
fi

# Infrastructure report
echo ""
echo "    === Infrastructure Report ==="
_ram_total=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{printf "%.0fGB", $2/1048576}')
_disk_free=$(df -h "$HOME" 2>/dev/null | tail -1 | awk '{print $4}')
_cgroup_mem=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo "unlimited")
if [ "$_cgroup_mem" != "max" ] && [ "$_cgroup_mem" != "unlimited" ]; then
    _cgroup_mem="$((${_cgroup_mem} / 1073741824))GB"
else
    _cgroup_mem="unlimited"
fi
echo "    RAM: $_ram_total (cgroup limit: $_cgroup_mem)"
echo "    Disk: $_disk_free free on \$HOME"
if [ -n "${TMPDIR:-}" ]; then
    echo "    TMPDIR: $TMPDIR (redirected from /tmp)"
fi

# Detect container environment
if [ -f /run/host/container-manager ] || grep -q "lxc" /proc/1/environ 2>/dev/null || [ -f /.dockerenv ]; then
    echo ""
    echo "    NOTE: Running inside a container. For full hardening:"
    echo "      incus config set <container> limits.memory 24GiB"
    echo "      incus config set <container> limits.cpu 8"
    echo "      incus config device set <container> root limits.read 190MB"
    echo "      incus config device set <container> root limits.write 90MB"
fi
echo ""


# ══════════════════════════════════════════════════════════════
#  Step 12 — Claude Code install + login
# ══════════════════════════════════════════════════════════════
echo "  [12/$TOTAL_STEPS] Setting up Claude Code..."

if command -v claude &>/dev/null; then
    cc_ver=$(claude --version 2>/dev/null || echo "unknown")
    echo "    . Claude Code already installed ($cc_ver)"
else
    echo "    Installing Claude Code..."
    if curl -fsSL https://claude.ai/install.sh | sh; then
        export PATH="$HOME/.local/bin:$PATH"
        # Persist PATH addition
        if ! grep -q '.local/bin' "$HOME/.bashrc" 2>/dev/null; then
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
        fi
        cc_ver=$(claude --version 2>/dev/null || echo "unknown")
        echo "    + Claude Code installed ($cc_ver)"
    else
        echo "    WARNING: Claude Code installation failed"
        echo "    Install manually: curl -fsSL https://claude.ai/install.sh | sh"
        SETUP_WARNINGS=1
    fi
fi

# Login guidance (interactive only)
if should_prompt && command -v claude &>/dev/null; then
    # Check if already logged in
    if claude auth status &>/dev/null 2>&1; then
        echo "    . Claude Code already authenticated"
    else
        echo ""
        echo "    Claude Code needs authentication."
        if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
            # Headless environment
            echo "    Since you're on a headless machine:"
            echo "      1. Run: claude login"
            echo "      2. It will print a URL — open it in YOUR browser"
            echo "      3. Complete the OAuth flow in your browser"
            echo "      4. Come back here — Claude Code will detect the login"
        else
            echo "    A browser window will open for OAuth login."
        fi
        echo ""
        read -rp "    Run 'claude login' now? [Y/n] " _cc_login
        if [ "${_cc_login:-Y}" != "n" ] && [ "${_cc_login:-Y}" != "N" ]; then
            claude login || echo "    WARNING: Login failed or was skipped"
        else
            echo "    Skipped. Run 'claude login' later to enable Genesis hooks."
        fi
    fi
fi
echo ""


# ══════════════════════════════════════════════════════════════
#  Step 13 — Interactive API key setup
# ══════════════════════════════════════════════════════════════
echo "  [13/$TOTAL_STEPS] API key configuration..."

# Count existing keys
_has_keys=0
if [ -f "$SECRETS_FILE" ]; then
    _has_keys=$(grep -E '^(API_KEY_|ANTHROPIC_API_KEY|GOOGLE_API_KEY|OPENAI_API_KEY)\w*=.+' "$SECRETS_FILE" 2>/dev/null | grep -cvE '=(your-|changeme|TODO|PLACEHOLDER|sk-xxx)' 2>/dev/null || echo "0")
fi

if should_prompt && { [ "$_has_keys" = "0" ] || [ "$INTERACTIVE" = "on" ]; }; then
    echo ""
    echo "    Genesis needs cloud API keys to function."
    echo "    Minimum: one LLM provider + one embedding provider."
    echo ""
    echo "    Recommended (covers most call sites with two keys):"
    echo "      - OpenRouter: LLM routing to 200+ models (openrouter.ai/keys)"
    echo "      - DeepInfra:  Cloud embeddings (deepinfra.com → Dashboard → API Keys)"
    echo ""

    # OpenRouter
    read -rp "    OpenRouter API key (or Enter to skip): " _or_key
    if [ -n "$_or_key" ]; then
        # Validate
        if curl -sf -H "Authorization: Bearer $_or_key" "https://openrouter.ai/api/v1/models" 2>/dev/null | head -c 20 | grep -q "data" 2>/dev/null; then
            echo "    + OpenRouter key validated"
        else
            echo "    ! Could not validate key (continuing anyway)"
        fi
        set_secret "API_KEY_OPENROUTER" "$_or_key" "$SECRETS_FILE"
        echo "    + Saved to secrets.env"
    fi

    # DeepInfra
    read -rp "    DeepInfra API key (or Enter to skip): " _di_key
    if [ -n "$_di_key" ]; then
        set_secret "API_KEY_DEEPINFRA" "$_di_key" "$SECRETS_FILE"
        echo "    + Saved to secrets.env"
    fi

    # Telegram (optional)
    echo ""
    read -rp "    Telegram bot token (or Enter to skip): " _tg_token
    if [ -n "$_tg_token" ]; then
        # Validate
        if curl -sf "https://api.telegram.org/bot${_tg_token}/getMe" 2>/dev/null | grep -q '"ok":true' 2>/dev/null; then
            echo "    + Telegram bot token validated"
        else
            echo "    ! Could not validate token (continuing anyway)"
        fi
        set_secret "TELEGRAM_BOT_TOKEN" "$_tg_token" "$SECRETS_FILE"
        echo "    + Saved to secrets.env"
    fi

    echo ""
    echo "    You can add more providers later by editing: $SECRETS_FILE"
    echo "    Or let Genesis guide you: on first CC session, Genesis will run"
    echo "    an interactive onboarding that configures and verifies everything."
elif [ "$_has_keys" -gt 0 ] 2>/dev/null; then
    echo "    . $_has_keys API key(s) already configured"
else
    echo "    - Skipped (non-interactive mode)."
    echo "    - On first CC session, Genesis will guide you through setup interactively."
    echo "    - Or edit secrets.env manually: $SECRETS_FILE"
fi
echo ""


# ══════════════════════════════════════════════════════════════
#  Timezone configuration
# ══════════════════════════════════════════════════════════════
echo "  Configuring timezone..."

GENESIS_TIMEZONE=""
if [ -f "$SECRETS_FILE" ]; then
    GENESIS_TIMEZONE=$(grep -oP '^USER_TIMEZONE=\K.*' "$SECRETS_FILE" 2>/dev/null || true)
    [ -z "$GENESIS_TIMEZONE" ] && GENESIS_TIMEZONE=$(grep -oP '^GENESIS_TIMEZONE=\K.*' "$SECRETS_FILE" 2>/dev/null || true)
fi
if [ -z "$GENESIS_TIMEZONE" ]; then
    CURRENT_TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo "UTC")
    if should_prompt; then
        echo "    Current timezone: $CURRENT_TZ"
        read -rp "    Enter timezone (e.g. America/New_York) or press Enter to keep [$CURRENT_TZ]: " INPUT_TZ
        GENESIS_TIMEZONE="${INPUT_TZ:-$CURRENT_TZ}"
    else
        GENESIS_TIMEZONE="$CURRENT_TZ"
        echo "    Using timezone: $GENESIS_TIMEZONE (non-interactive)"
    fi
fi
if command -v timedatectl &>/dev/null; then
    sudo timedatectl set-timezone "$GENESIS_TIMEZONE" 2>/dev/null && \
        echo "    + System timezone set to $GENESIS_TIMEZONE" || \
        echo "    WARNING: Could not set timezone. Set manually: sudo timedatectl set-timezone $GENESIS_TIMEZONE"
fi
if [ -f "$SECRETS_FILE" ] && ! grep -q "^USER_TIMEZONE=" "$SECRETS_FILE" 2>/dev/null; then
    set_secret "USER_TIMEZONE" "$GENESIS_TIMEZONE" "$SECRETS_FILE"
    echo "    + Saved to secrets.env"
fi
echo ""


# ══════════════════════════════════════════════════════════════
#  Step 14 — Verify + Smoke test
# ══════════════════════════════════════════════════════════════
echo "  [14/$TOTAL_STEPS] Verifying installation..."

SMOKE_PASS=0
SMOKE_FAIL=0
SMOKE_SKIP=0

# Genesis import check
VERIFY_PYTHON="${VENV_PATH}/bin/python"
if [ -x "$VERIFY_PYTHON" ]; then
    if "$VERIFY_PYTHON" -c "from genesis.runtime import GenesisRuntime" 2>/dev/null; then
        echo "    PASS  Genesis import"
        SMOKE_PASS=$((SMOKE_PASS + 1))
    else
        echo "    FAIL  Genesis import — check pip install output above"
        SMOKE_FAIL=$((SMOKE_FAIL + 1))
    fi
else
    echo "    SKIP  Genesis import (venv not found)"
    SMOKE_SKIP=$((SMOKE_SKIP + 1))
fi

# Plugin dirs
for plugin_dir in genesis genesis-memory; do
    if [ -d "$AZ_ROOT/usr/plugins/$plugin_dir" ]; then
        echo "    PASS  Plugin: $plugin_dir"
        SMOKE_PASS=$((SMOKE_PASS + 1))
    else
        echo "    FAIL  Plugin: $plugin_dir not found"
        SMOKE_FAIL=$((SMOKE_FAIL + 1))
    fi
done

# server_startup hook
if grep -q "server_startup" "$RUN_UI" 2>/dev/null; then
    echo "    PASS  server_startup hook in run_ui.py"
    SMOKE_PASS=$((SMOKE_PASS + 1))
else
    echo "    FAIL  server_startup hook missing from run_ui.py"
    SMOKE_FAIL=$((SMOKE_FAIL + 1))
fi

# Qdrant
if curl -sf "$QDRANT_URL/collections" >/dev/null 2>&1; then
    echo "    PASS  Qdrant reachable"
    SMOKE_PASS=$((SMOKE_PASS + 1))
else
    echo "    FAIL  Qdrant not reachable at $QDRANT_URL"
    SMOKE_FAIL=$((SMOKE_FAIL + 1))
fi

# Claude Code
if command -v claude &>/dev/null; then
    echo "    PASS  Claude Code installed"
    SMOKE_PASS=$((SMOKE_PASS + 1))
else
    echo "    WARN  Claude Code not installed"
    SMOKE_SKIP=$((SMOKE_SKIP + 1))
fi

# MCP config
if [ -f "$REPO_DIR/.mcp.json" ]; then
    echo "    PASS  .mcp.json"
    SMOKE_PASS=$((SMOKE_PASS + 1))
else
    echo "    FAIL  .mcp.json not found (MCP tools won't work)"
    SMOKE_FAIL=$((SMOKE_FAIL + 1))
fi

# Claude Code hooks config
if [ -f "$REPO_DIR/.claude/settings.json" ]; then
    echo "    PASS  .claude/settings.json (hooks)"
    SMOKE_PASS=$((SMOKE_PASS + 1))
else
    echo "    FAIL  .claude/settings.json not found (hooks won't work)"
    SMOKE_FAIL=$((SMOKE_FAIL + 1))
fi

# Systemd services (template-generated)
for _svc in agent-zero genesis-bridge genesis-watchdog; do
    if [ -f "$SYSTEMD_USER_DIR/${_svc}.service" ]; then
        echo "    PASS  ${_svc}.service"
        SMOKE_PASS=$((SMOKE_PASS + 1))
    else
        echo "    SKIP  ${_svc}.service"
        SMOKE_SKIP=$((SMOKE_SKIP + 1))
    fi
done

# API keys configured
_recheck_keys=0
if [ -f "$SECRETS_FILE" ]; then
    _recheck_keys=$(grep -E '^(API_KEY_|ANTHROPIC_API_KEY|GOOGLE_API_KEY|OPENAI_API_KEY)\w*=.+' "$SECRETS_FILE" 2>/dev/null | grep -cvE '=(your-|changeme|TODO|PLACEHOLDER|sk-xxx)' 2>/dev/null || echo "0")
fi
if [ "$_recheck_keys" -gt 0 ] 2>/dev/null; then
    echo "    PASS  API keys: $_recheck_keys configured"
    SMOKE_PASS=$((SMOKE_PASS + 1))
else
    echo "    WARN  No API keys configured yet"
    SMOKE_SKIP=$((SMOKE_SKIP + 1))
fi

# Embedding test (only if Qdrant is up and keys exist)
if [ "$_recheck_keys" -gt 0 ] 2>/dev/null && curl -sf "$QDRANT_URL/collections" >/dev/null 2>&1 && [ -x "$VERIFY_PYTHON" ]; then
    if GENESIS_SECRETS="$SECRETS_FILE" "$VERIFY_PYTHON" -c "
import asyncio, os
from dotenv import load_dotenv
load_dotenv(os.environ['GENESIS_SECRETS'])
from genesis.memory.embeddings import EmbeddingProvider
async def t():
    p = EmbeddingProvider()
    v = await p.embed('smoke test')
    assert len(v) > 0, 'empty embedding'
asyncio.run(t())
" 2>/dev/null; then
        echo "    PASS  Embedding provider"
        SMOKE_PASS=$((SMOKE_PASS + 1))
    else
        echo "    WARN  Embedding test failed (may need API keys)"
        SMOKE_SKIP=$((SMOKE_SKIP + 1))
    fi
else
    echo "    SKIP  Embedding test (prerequisites not met)"
    SMOKE_SKIP=$((SMOKE_SKIP + 1))
fi

# Summary
echo ""
echo "    ── Smoke Test Summary ──"
echo "    $SMOKE_PASS passed, $SMOKE_FAIL failed, $SMOKE_SKIP skipped"


# ══════════════════════════════════════════════════════════════
#  Done
# ══════════════════════════════════════════════════════════════
echo ""
echo "  ─────────────────────────────────────────"
if [ "$SMOKE_FAIL" -gt 0 ]; then
    echo "  Setup complete (with failures — see above)."
elif [ "${SETUP_WARNINGS:-0}" = "1" ]; then
    echo "  Setup complete (with warnings — see above)."
else
    echo "  Setup complete!"
fi

# Recount keys for final message
_final_keys=$(grep -cE '^(API_KEY_|ANTHROPIC_API_KEY|GOOGLE_API_KEY|OPENAI_API_KEY)\w*=.+' "$SECRETS_FILE" 2>/dev/null || echo "0")

echo ""
echo "  Next steps:"
echo "    1. Start Genesis:  systemctl --user start agent-zero"
echo "    2. Run:  claude"
echo "       Genesis will guide you through setup on first launch."
if [ "$_final_keys" = "0" ] 2>/dev/null; then
    echo "       (API keys, user profile, channels — all handled interactively)"
fi
echo "    3. Web UI: http://localhost:5000"
echo ""
echo "  Manage services:"
echo "    systemctl --user start|stop|restart agent-zero"
echo "    systemctl --user start|stop|restart qdrant"
echo "    journalctl --user -u agent-zero -f    # live logs"
echo ""
echo "  Documentation:"
echo "    CLAUDE.md            — conventions & commands"
echo "    docs/architecture/   — system design"
echo ""
