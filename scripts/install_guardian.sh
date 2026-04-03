#!/usr/bin/env bash
# install_guardian.sh — Install and configure the Genesis Guardian on the host VM.
#
# Auto-detects the container name, IP, and all other settings. No manual
# configuration required — runs as part of host-setup.sh or standalone.
#
# This script copies code from the local repo (no internet required).
# It also sets up bidirectional SSH so the container can manage Guardian
# health via the guardian-gateway.sh command dispatcher.
#
# Prerequisites:
#   - Python 3.12+ on host
#   - incus CLI configured and working
#   - Genesis container running
#   - sudo access (for loginctl enable-linger)
#
# Usage:
#   bash install_guardian.sh [options]
#
# Options:
#   --container-name NAME  Container name (default: auto-detect or "genesis")
#   --non-interactive      Skip prompts
#   -h, --help             Show this help
#
# This script is idempotent — safe to run multiple times.

set -euo pipefail

# ── Defaults & arg parsing ────────────────────────────────────────────

CONTAINER_NAME=""
NON_INTERACTIVE=0
INSTALL_DIR="$HOME/.local/share/genesis-guardian"
STATE_DIR="$HOME/.local/state/genesis-guardian"
SYSTEMD_DIR="$HOME/.config/systemd/user"
CONTAINER_USER="${CONTAINER_USER:-ubuntu}"
CONTAINER_HOME="/home/$CONTAINER_USER"

while [ $# -gt 0 ]; do
    case "$1" in
        --container-name) [ $# -ge 2 ] || { echo "ERROR: $1 requires a value"; exit 1; }; CONTAINER_NAME="$2"; shift ;;
        --repo-url)       echo "WARN: --repo-url is deprecated (code is copied from local repo)"; shift ;;
        --non-interactive) NON_INTERACTIVE=1 ;;
        -h|--help)
            sed -n '2,/^$/{ s/^# \?//; p }' "$0"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

# ── Auto-detect container ─────────────────────────────────────────────

if [ -z "$CONTAINER_NAME" ]; then
    # Try to find a running container named "genesis"
    if incus info genesis &>/dev/null; then
        CONTAINER_NAME="genesis"
    else
        # Find first running container
        CONTAINER_NAME=$(incus list status=running -f csv -c n 2>/dev/null | head -1 || echo "")
        if [ -z "$CONTAINER_NAME" ]; then
            echo "ERROR: No running containers found. Start the Genesis container first."
            exit 1
        fi
        echo "  Auto-detected container: $CONTAINER_NAME"
    fi
fi

# Auto-detect container IP
# Prefer eth0 (Incus bridge) over Tailscale or other interfaces
CONTAINER_IP=$(incus list "$CONTAINER_NAME" -f csv -c 4 2>/dev/null \
    | grep -oP '\d+\.\d+\.\d+\.\d+(?=\s*\(eth0\))' | head -1 || echo "")
if [ -z "$CONTAINER_IP" ]; then
    # Fallback: first available IP
    CONTAINER_IP=$(incus list "$CONTAINER_NAME" -f csv -c 4 2>/dev/null \
        | grep -oP '\d+\.\d+\.\d+\.\d+' | head -1 || echo "")
fi
if [ -z "$CONTAINER_IP" ]; then
    echo "ERROR: Cannot detect IP for container '$CONTAINER_NAME'"
    echo "  Is the container running? Try: incus start $CONTAINER_NAME"
    exit 1
fi

# Auto-detect health API port (default 5000)
HEALTH_PORT=5000

VENV_DIR="$INSTALL_DIR/.venv"

echo ""
echo "  Genesis Guardian Installer"
echo "  ─────────────────────────────────────────"
echo "  Container: $CONTAINER_NAME"
echo "  IP:        $CONTAINER_IP"
echo "  Install:   $INSTALL_DIR"
echo ""

# ── Step 1: Verify prerequisites ─────────────────────────────────────

echo "[1/13] Checking prerequisites..."

# Python 3.12+
PYTHON=$(command -v python3.12 || command -v python3 || true)
if [ -z "$PYTHON" ]; then
    echo "  FAIL  Python 3.12+ not found"
    exit 1
fi
PYTHON_VERSION=$($PYTHON --version 2>&1 | grep -oP '\d+\.\d+')
echo "  OK    Python $PYTHON_VERSION"

# incus (already used above, so it exists)
echo "  OK    incus: $(incus version 2>/dev/null || echo 'available')"
echo "  OK    Container '$CONTAINER_NAME': $CONTAINER_IP"

# Tailscale (optional, preferred for approval URLs)
if command -v tailscale &>/dev/null; then
    TS_IP=$(tailscale ip -4 2>/dev/null || echo "")
    if [ -n "$TS_IP" ]; then
        echo "  OK    Tailscale: $TS_IP (will use for approval URLs)"
    else
        echo "  WARN  Tailscale running but no IPv4"
    fi
fi

# Fallback: use host LAN IP if no Tailscale
if [ -z "${TS_IP:-}" ]; then
    TS_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || echo "")
    if [ -n "$TS_IP" ]; then
        echo "  OK    Host LAN IP: $TS_IP (will use for approval URLs)"
    else
        echo "  WARN  Could not detect host IP — set approval.bind_host manually"
    fi
fi

# Claude CLI (optional)
CLAUDE_PATH=$(command -v claude 2>/dev/null)
if [ -n "$CLAUDE_PATH" ] && [ -f "$CLAUDE_PATH" ]; then
    echo "  OK    Claude CLI: $CLAUDE_PATH"
    CC_ENABLED=true
    # Check auth status (report-only, non-interactive)
    if "$CLAUDE_PATH" auth status &>/dev/null; then
        echo "  OK    Claude Code: authenticated"
    else
        echo "  WARN  Claude Code: not authenticated — run 'claude login' on the host"
        echo "        Guardian CC diagnosis requires authentication"
    fi
else
    echo "  WARN  Claude CLI not found — CC diagnosis disabled"
    CC_ENABLED=false
    CLAUDE_PATH="claude"
fi

# User lingering (required for persistent systemd user timers)
if loginctl show-user "$(whoami)" 2>/dev/null | grep -q "Linger=yes"; then
    echo "  OK    Linger enabled for $(whoami)"
else
    echo "  Enabling lingering for $(whoami) (required for persistent timers)..."
    if sudo loginctl enable-linger "$(whoami)" 2>/dev/null; then
        echo "  OK    Linger enabled"
    else
        echo "  WARN  Could not enable linger — Guardian timers will stop on logout"
        echo "        Run: sudo loginctl enable-linger $(whoami)"
    fi
fi

# ── Step 2: Copy code from local repo ────────────────────────────────
# The user already has the repo (they cloned it to run this script).
# No need for a second git clone — just copy from the local checkout.

echo ""
echo "[2/13] Setting up code..."

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# If already running from the install dir (e.g. host-setup.sh cloned directly
# into INSTALL_DIR), skip the copy — code is already in place.
if [ "$(cd "$REPO_ROOT" && pwd)" = "$(cd "$INSTALL_DIR" 2>/dev/null && pwd)" ] 2>/dev/null; then
    echo "  Code already in $INSTALL_DIR (same as repo)"
elif [ -d "$INSTALL_DIR/src/genesis/guardian" ]; then
    echo "  Updating from local repo: $REPO_ROOT"
    cp -r "$REPO_ROOT/src" "$INSTALL_DIR/src"
    cp -r "$REPO_ROOT/config" "$INSTALL_DIR/config"
    cp -r "$REPO_ROOT/scripts" "$INSTALL_DIR/scripts"
else
    echo "  Copying from local repo: $REPO_ROOT"
    mkdir -p "$INSTALL_DIR"
    cp -r "$REPO_ROOT/src" "$INSTALL_DIR/src"
    cp -r "$REPO_ROOT/config" "$INSTALL_DIR/config"
    cp -r "$REPO_ROOT/scripts" "$INSTALL_DIR/scripts"
fi

# ── Step 3: Create venv ──────────────────────────────────────────────

echo ""
echo "[3/13] Creating virtual environment..."

if [ ! -d "$VENV_DIR" ]; then
    $PYTHON -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet pyyaml
echo "  venv: $VENV_DIR"

# ── Step 4: Create state & shared directories ───────────────────────

echo ""
echo "[4/13] Creating state & shared directories..."

mkdir -p "$STATE_DIR"
mkdir -p "$STATE_DIR/shared/briefing"   # Genesis writes, Guardian reads
mkdir -p "$STATE_DIR/shared/guardian"    # Guardian writes, Genesis reads (Phase 2)
echo "  State dir: $STATE_DIR"
echo "  Shared:    $STATE_DIR/shared/{briefing,guardian}"

# ── Step 5: Generate guardian.yaml with detected values ───────────────

echo ""
echo "[5/13] Generating guardian.yaml..."

cat > "$INSTALL_DIR/config/guardian.yaml" << YAML
# Auto-generated by install_guardian.sh — $(date -Is)
# Edit these values if your environment changes.

container_name: "$CONTAINER_NAME"
container_ip: "$CONTAINER_IP"
health_api_port: $HEALTH_PORT

# Host VM details — used by Genesis for bidirectional monitoring (SSH → gateway)
host_ip: "${TS_IP:-}"
host_user: "$(whoami)"

probes:
  probe_timeout_s: 10
  ping_count: 1
  ping_timeout_s: 3

confirmation:
  recheck_delay_s: 30
  max_recheck_attempts: 3
  required_failed_signals: 2
  bootstrap_grace_s: 300

recovery:
  verification_delay_s: 30
  max_escalations: 3

snapshots:
  retention: 5
  prefix: "guardian-"

approval:
  port: 8888
  token_expiry_s: 86400
  bind_host: "${TS_IP:-}"

briefing:
  enabled: true
  shared_subdir: "shared"
  briefing_filename: "guardian_briefing.md"
  max_age_s: 600

cc:
  enabled: $CC_ENABLED
  model: opus
  timeout_s: 3600
  max_turns: 50
  path: "$CLAUDE_PATH"
YAML
echo "  Generated with container=$CONTAINER_NAME ip=$CONTAINER_IP"
if [ -n "${TS_IP:-}" ]; then
    echo "  Approval URLs will use Tailscale IP: $TS_IP"
else
    echo "  WARNING: No Tailscale IP — approval URLs will use 'localhost'"
    echo "  Set approval.bind_host in guardian.yaml to a reachable IP"
fi

# ── Step 6: Telegram credential bridge ────────────────────────────────
#
# Telegram credentials are auto-propagated from the container via the shared
# Incus mount. The container's awareness loop extracts ONLY the Telegram keys
# from secrets.env and writes them to shared/guardian/telegram_creds.env.
# Guardian reads from there. The full secrets file never leaves the container.

echo ""
echo "[6/13] Telegram credential bridge..."

CREDS_FILE="$STATE_DIR/shared/guardian/telegram_creds.env"
if [ -f "$CREDS_FILE" ]; then
    echo "  Telegram credentials found on shared mount: $CREDS_FILE"
    if grep -q "TELEGRAM_BOT_TOKEN" "$CREDS_FILE" 2>/dev/null; then
        echo "  Bot token present — Telegram alerts should work"
    else
        echo "  WARNING: Credential file exists but has no bot token"
    fi
else
    echo "  Telegram credentials not yet on shared mount."
    echo "  They auto-propagate from the container within 5 minutes."
    echo ""
    echo "  To set up Telegram alerts:"
    echo "    1. Create a bot via @BotFather on Telegram"
    echo "    2. Add to container secrets: incus exec $CONTAINER_NAME -- nano ~/genesis/secrets.env"
    echo "       TELEGRAM_BOT_TOKEN=<your bot token>"
    echo "       TELEGRAM_FORUM_CHAT_ID=<your chat/group ID>"
    echo "    3. Credentials propagate to Guardian on the next awareness tick (~5 min)"
fi

# Keep legacy secrets.env for backward compatibility if it exists
LEGACY_SECRETS="$INSTALL_DIR/secrets.env"
if [ -f "$LEGACY_SECRETS" ]; then
    echo "  NOTE: Legacy secrets.env found at $LEGACY_SECRETS"
    echo "  Shared mount credentials take priority when available."
fi

# ── Step 7: Generate CLAUDE.md for diagnostic CC ──────────────────────

echo ""
echo "[7/13] Generating CLAUDE.md for diagnostic CC..."

# Single source of truth: config/guardian-claude.md
# The gateway update command also copies this file after every git pull.
if [ -f "$INSTALL_DIR/config/guardian-claude.md" ]; then
    cp "$INSTALL_DIR/config/guardian-claude.md" "$INSTALL_DIR/CLAUDE.md"
else
    echo "  WARNING: config/guardian-claude.md not found — CLAUDE.md not generated"
fi
echo "  CLAUDE.md generated"

# ── Step 8: Install systemd units ─────────────────────────────────────

echo ""
echo "[8/13] Installing systemd units..."

mkdir -p "$SYSTEMD_DIR"

for unit in genesis-guardian.service genesis-guardian.timer \
            genesis-guardian-watchman.service genesis-guardian-watchman.timer; do
    cp "$INSTALL_DIR/config/$unit" "$SYSTEMD_DIR/$unit"
    echo "  Installed $unit"
done

systemctl --user daemon-reload
echo "  Reloaded systemd"

# ── Step 9: Install gateway script ────────────────────────────────────

echo ""
echo "[9/13] Installing guardian gateway..."

mkdir -p "$HOME/.local/bin"
cp "$INSTALL_DIR/scripts/guardian-gateway.sh" "$HOME/.local/bin/guardian-gateway.sh"
chmod +x "$HOME/.local/bin/guardian-gateway.sh"
echo "  Installed guardian-gateway.sh"

# ── Step 10: Set up bidirectional SSH ────────────────────────────────
# Generate a dedicated SSH key in the container, then install a
# command-restricted authorized_keys entry on this host so Genesis
# can call the gateway script (restart-timer, pause, resume, status).

echo ""
echo "[10/13] Setting up bidirectional SSH..."

GUARDIAN_KEY="/home/${CONTAINER_USER}/.ssh/genesis_guardian_ed25519"

# Generate dedicated keypair in container (if not already present)
if incus exec "$CONTAINER_NAME" -- test -f "$GUARDIAN_KEY" 2>/dev/null; then
    echo "  Guardian SSH key already exists in container"
else
    incus exec "$CONTAINER_NAME" -- su - "$CONTAINER_USER" -c \
        "ssh-keygen -t ed25519 -f $GUARDIAN_KEY -N '' -C 'genesis-guardian-control'" 2>/dev/null
    echo "  Generated guardian SSH keypair in container"
fi

# Pull public key from container
PUBKEY=$(incus exec "$CONTAINER_NAME" -- cat "${GUARDIAN_KEY}.pub" 2>/dev/null || echo "")
if [ -z "$PUBKEY" ]; then
    echo "  WARNING: Could not read guardian public key — bidirectional monitoring disabled"
else
    # Install command-restricted authorized_keys entry (idempotent by key material)
    PUBKEY_BLOB=$(echo "$PUBKEY" | awk '{print $2}')
    if grep -qF "$PUBKEY_BLOB" "$HOME/.ssh/authorized_keys" 2>/dev/null; then
        echo "  Authorized key already installed (key matches)"
    else
        # Remove stale entry (same comment, different key) if present
        if grep -q "genesis-guardian-control" "$HOME/.ssh/authorized_keys" 2>/dev/null; then
            sed -i '/genesis-guardian-control/d' "$HOME/.ssh/authorized_keys"
            echo "  Removed stale guardian key (container rebuilt)"
        fi
        mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
        echo "command=\"$HOME/.local/bin/guardian-gateway.sh\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding ${PUBKEY}" >> "$HOME/.ssh/authorized_keys"
        chmod 600 "$HOME/.ssh/authorized_keys"
        echo "  Installed command-restricted SSH key for Genesis→Guardian control"
    fi

    # Write connection config into container for Genesis to read
    HOST_USER="$(whoami)"
    HOST_IP="${TS_IP:-}"
    incus exec "$CONTAINER_NAME" -- su - "$CONTAINER_USER" -c \
        "mkdir -p ~/.genesis && cat > ~/.genesis/guardian_remote.yaml" <<REMOTECONF
# Auto-generated by install_guardian.sh — $(date -Is)
# Genesis reads this to SSH to the host for Guardian management.
host_ip: "${HOST_IP}"
host_user: "${HOST_USER}"
ssh_key: "~/.ssh/genesis_guardian_ed25519"
REMOTECONF
    echo "  Wrote guardian_remote.yaml into container"

    # Verify SSH connectivity: container → host
    echo "  Testing SSH connectivity (container → host)..."
    CONTAINER_IP=$(incus list "$CONTAINER_NAME" -f csv -c 4 2>/dev/null | grep -oP '[\d.]+' | head -1)
    SSH_TEST=$(incus exec "$CONTAINER_NAME" -- su - "$CONTAINER_USER" -c \
        "ssh -i ${GUARDIAN_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=5 ${HOST_USER}@${HOST_IP} 'echo ok' 2>&1" || echo "FAILED")
    if echo "$SSH_TEST" | grep -q "^ok"; then
        echo "  SSH bidirectional link verified"
    else
        echo ""
        echo "  WARNING: SSH test failed. Guardian bidirectional monitoring may not work."
        echo "  Error: $SSH_TEST"
        echo ""
        echo "  To fix manually, add this to ~/.ssh/authorized_keys on the host:"
        echo "    command=\"\$HOME/.local/bin/guardian-gateway.sh\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding ${PUBKEY}"
        echo ""
    fi
fi

# ── Step 11: Configure shared filesystem mounts ─────────────────────
# Incus disk devices give Genesis and Guardian a shared data plane.
# Genesis writes briefings; Guardian reads them before CC diagnosis.
# Uses shift=true for transparent UID mapping (requires kernel 5.12+).

echo ""
echo "[11/13] Configuring shared filesystem mounts..."

SHARED_DIR="$STATE_DIR/shared"

# Create container-side mount point
incus exec "$CONTAINER_NAME" -- su - "$CONTAINER_USER" -c \
    "mkdir -p ~/.genesis/shared/briefing ~/.genesis/shared/guardian" 2>/dev/null

# Add Incus disk device for the shared directory (idempotent — remove first if exists)
if incus config device get "$CONTAINER_NAME" guardian-shared source &>/dev/null; then
    echo "  Shared mount already configured — updating"
    incus config device remove "$CONTAINER_NAME" guardian-shared 2>/dev/null || true
fi

MOUNT_ERR=$(incus config device add "$CONTAINER_NAME" guardian-shared disk \
    source="$SHARED_DIR" \
    path="$CONTAINER_HOME/.genesis/shared" \
    shift=true 2>&1)
if [ $? -eq 0 ]; then
    echo "  OK    Incus shared mount: $SHARED_DIR → $CONTAINER_HOME/.genesis/shared"

    # Verify: write from host, read from container
    echo "mount-test-$(date +%s)" > "$SHARED_DIR/briefing/.mount-test"
    if incus exec "$CONTAINER_NAME" -- cat "$CONTAINER_HOME/.genesis/shared/briefing/.mount-test" &>/dev/null; then
        echo "  OK    Mount verified (host→container read works)"
        rm -f "$SHARED_DIR/briefing/.mount-test"
    else
        echo "  WARN  Mount created but verification failed — check UID mapping"
        echo "        Try: incus config set $CONTAINER_NAME raw.idmap 'both 1000 1000'"
        rm -f "$SHARED_DIR/briefing/.mount-test"
    fi
else
    echo "  WARN  Could not create Incus shared mount: $MOUNT_ERR"
    echo "        Guardian will work without shared filesystem (no briefing context)"
    echo "        Manual setup: incus config device add $CONTAINER_NAME guardian-shared disk \\"
    echo "          source=$SHARED_DIR path=$CONTAINER_HOME/.genesis/shared shift=true"
fi

# ── Step 12: Enable timers ────────────────────────────────────────────

echo ""
echo "[12/13] Enabling timers..."

systemctl --user enable genesis-guardian.timer
systemctl --user start genesis-guardian.timer
echo "  Guardian timer: enabled + started"

systemctl --user enable genesis-guardian-watchman.timer
systemctl --user start genesis-guardian-watchman.timer
echo "  Watchman timer: enabled + started"

# ── Step 13: Verification ────────────────────────────────────────────

echo ""
echo "[13/13] Running verification..."

echo ""
echo "  Timer status:"
systemctl --user status genesis-guardian.timer --no-pager 2>&1 | head -5 || true

echo ""
echo "  Running initial health check..."
PYTHONPATH="$INSTALL_DIR/src" \
    GUARDIAN_CONFIG="$INSTALL_DIR/config/guardian.yaml" \
    "$VENV_DIR/bin/python" -m genesis.guardian --check-only 2>&1 | head -20 || \
    echo "  WARNING: Initial check produced errors (may need container to be fully up)"

echo ""
echo "  ─────────────────────────────────────────"
echo "  Guardian installed successfully"
echo ""
echo "  Monitoring: $CONTAINER_NAME ($CONTAINER_IP:$HEALTH_PORT)"
echo ""
echo "  Commands:"
echo "    systemctl --user status genesis-guardian.timer   # Check timer"
echo "    journalctl --user -u genesis-guardian -f          # Follow logs"
echo "    $VENV_DIR/bin/python -m genesis.guardian          # Manual run"
echo ""
