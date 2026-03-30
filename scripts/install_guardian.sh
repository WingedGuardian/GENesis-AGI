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

echo "[1/12] Checking prerequisites..."

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
CLAUDE_PATH=$(command -v claude || echo "$HOME/.local/bin/claude")
if [ -f "$CLAUDE_PATH" ]; then
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
    CLAUDE_PATH="~/.local/bin/claude"
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
echo "[2/12] Setting up code..."

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
echo "[3/12] Creating virtual environment..."

if [ ! -d "$VENV_DIR" ]; then
    $PYTHON -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet pyyaml
echo "  venv: $VENV_DIR"

# ── Step 4: Create state directory ────────────────────────────────────

echo ""
echo "[4/12] Creating state directory..."

mkdir -p "$STATE_DIR"
echo "  State dir: $STATE_DIR"

# ── Step 5: Generate guardian.yaml with detected values ───────────────

echo ""
echo "[5/12] Generating guardian.yaml..."

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

cc:
  enabled: $CC_ENABLED
  model: sonnet
  timeout_s: 120
  path: "$CLAUDE_PATH"
YAML
echo "  Generated with container=$CONTAINER_NAME ip=$CONTAINER_IP"
if [ -n "${TS_IP:-}" ]; then
    echo "  Approval URLs will use Tailscale IP: $TS_IP"
else
    echo "  WARNING: No Tailscale IP — approval URLs will use 'localhost'"
    echo "  Set approval.bind_host in guardian.yaml to a reachable IP"
fi

# ── Step 6: Copy secrets from container ───────────────────────────────

echo ""
echo "[6/12] Copying secrets..."

SECRETS_FILE="$INSTALL_DIR/secrets.env"
if [ ! -f "$SECRETS_FILE" ]; then
    if incus file pull "$CONTAINER_NAME$CONTAINER_HOME/genesis/secrets.env" "$SECRETS_FILE" 2>/dev/null; then
        chmod 600 "$SECRETS_FILE"
        echo "  Copied secrets.env from container"
    else
        echo "  WARNING: Could not copy secrets.env — Telegram alerts won't work"
        echo "  Create $SECRETS_FILE with TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
    fi
else
    echo "  secrets.env already exists"
fi

# ── Step 7: Generate CLAUDE.md for diagnostic CC ──────────────────────

echo ""
echo "[7/12] Generating CLAUDE.md for diagnostic CC..."

cat > "$INSTALL_DIR/CLAUDE.md" << 'CLAUDEMD'
# Genesis Guardian — Diagnostic Brain

## PRIME DIRECTIVE: First, do no harm.

Any single signal can lie. You exist to cross-reference multiple signals and
exercise judgment. Never recommend an action unless you are confident the
diagnosis is correct. A wrong recovery action on a healthy system is worse
than no action at all. When in doubt: ESCALATE to the human. You are a
doctor, not a script.

## Identity

You are Genesis's diagnostic brain — its immune system, not Genesis itself.
You are invoked ONLY when Genesis appears to be down. Your job: analyze
system diagnostics, identify the root cause, and recommend a specific
recovery action.

## Output Contract

You MUST produce valid JSON with this exact schema:

```json
{
  "likely_cause": "One-sentence description of the root cause",
  "confidence_pct": 85,
  "evidence": ["Evidence point 1", "Evidence point 2"],
  "recommended_action": "RESTART_SERVICES",
  "reasoning": "Multi-sentence explanation of your analysis"
}
```

## Available Recovery Actions

| Action | What it does |
|--------|-------------|
| RESTART_SERVICES | Restart genesis-bridge systemd service |
| RESOURCE_CLEAR | Clear /tmp, reclaim cache, restart |
| REVERT_CODE | Git revert HEAD, then restart |
| RESTART_CONTAINER | Full incus restart of the container |
| SNAPSHOT_ROLLBACK | Restore from last healthy snapshot |
| ESCALATE | Alert user, no automated action |

## Rules

- `recommended_action` MUST be one of the 6 actions above
- If confidence < 70%, set recommended_action to ESCALATE
- Never recommend raising resource limits
- Never recommend working around symptoms — fix the root cause
- Look at temporal patterns: what changed recently? what metric degraded first?
- Check the git state: was there a recent commit that could have caused this?
- Respond with ONLY the JSON object, no markdown fences, no explanation

## Genesis Context

Genesis is an autonomous AI agent running in an Incus container.
Key services: genesis-bridge (main process), qdrant (vector DB).
Data at: ~/genesis/data/genesis.db, ~/.genesis/status.json.
Awareness loop ticks every 5 minutes.
CLAUDEMD
echo "  CLAUDE.md generated"

# ── Step 8: Install systemd units ─────────────────────────────────────

echo ""
echo "[8/12] Installing systemd units..."

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
echo "[9/12] Installing guardian gateway..."

mkdir -p "$HOME/.local/bin"
cp "$INSTALL_DIR/scripts/guardian-gateway.sh" "$HOME/.local/bin/guardian-gateway.sh"
chmod +x "$HOME/.local/bin/guardian-gateway.sh"
echo "  Installed guardian-gateway.sh"

# ── Step 10: Set up bidirectional SSH ────────────────────────────────
# Generate a dedicated SSH key in the container, then install a
# command-restricted authorized_keys entry on this host so Genesis
# can call the gateway script (restart-timer, pause, resume, status).

echo ""
echo "[10/12] Setting up bidirectional SSH..."

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
    # Install command-restricted authorized_keys entry (idempotent)
    GATEWAY_PATH="\$HOME/.local/bin/guardian-gateway.sh"
    if ! grep -qF "genesis-guardian-control" "$HOME/.ssh/authorized_keys" 2>/dev/null; then
        mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
        echo "command=\"$HOME/.local/bin/guardian-gateway.sh\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding ${PUBKEY}" >> "$HOME/.ssh/authorized_keys"
        chmod 600 "$HOME/.ssh/authorized_keys"
        echo "  Installed command-restricted SSH key for Genesis→Guardian control"
    else
        echo "  Authorized key already installed"
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
fi

# ── Step 11: Enable timers ────────────────────────────────────────────

echo ""
echo "[11/12] Enabling timers..."

systemctl --user enable genesis-guardian.timer
systemctl --user start genesis-guardian.timer
echo "  Guardian timer: enabled + started"

systemctl --user enable genesis-guardian-watchman.timer
systemctl --user start genesis-guardian-watchman.timer
echo "  Watchman timer: enabled + started"

# ── Step 12: Verification ────────────────────────────────────────────

echo ""
echo "[12/12] Running verification..."

echo ""
echo "  Timer status:"
systemctl --user status genesis-guardian.timer --no-pager 2>&1 | head -5 || true

echo ""
echo "  Running initial health check..."
PYTHONPATH="$INSTALL_DIR/src" \
    GUARDIAN_CONFIG="$INSTALL_DIR/config/guardian.yaml" \
    GUARDIAN_SECRETS="$INSTALL_DIR/secrets.env" \
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
