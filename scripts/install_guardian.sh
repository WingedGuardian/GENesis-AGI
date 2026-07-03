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

echo "[1/14] Checking prerequisites..."

# Python 3 (Guardian only needs pyyaml — 3.11+ works fine)
PYTHON=$(command -v python3.12 || command -v python3 || true)
if [ -z "$PYTHON" ]; then
    echo "  FAIL  Python 3 not found"
    exit 1
fi
PYTHON_VERSION=$($PYTHON --version 2>&1 | grep -oP '\d+\.\d+')
echo "  OK    Python $PYTHON_VERSION"

# python3-venv (required for venv creation — ensurepip ships in the venv package)
if ! $PYTHON -c "import ensurepip" &>/dev/null; then
    VENV_PKG="python${PYTHON_VERSION}-venv"
    echo "  $VENV_PKG not found — installing..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq "$VENV_PKG" || {
            echo "  FAIL  Could not install $VENV_PKG"
            echo "  Install manually: sudo apt-get install $VENV_PKG"
            exit 1
        }
        echo "  OK    $VENV_PKG installed"
    else
        echo "  FAIL  $VENV_PKG not installed and apt-get not available"
        exit 1
    fi
fi

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
CC_AUTHENTICATED=false
if [ -n "$CLAUDE_PATH" ] && [ -f "$CLAUDE_PATH" ]; then
    echo "  OK    Claude CLI: $CLAUDE_PATH"
    CC_ENABLED=true
    if "$CLAUDE_PATH" auth status &>/dev/null; then
        echo "  OK    Claude Code: authenticated"
        CC_AUTHENTICATED=true
    else
        echo "  WARN  Claude Code: not authenticated (will prompt after prereqs)"
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

# ── Claude Code login prompt (Guardian) ──────────────────────────
# If CC is installed but not authenticated, prompt the user.
if [ "$CC_ENABLED" = "true" ] && [ "$CC_AUTHENTICATED" = "false" ] && [ "$NON_INTERACTIVE" != "1" ] && [ -t 0 ]; then
    echo ""
    echo "  ── Claude Code Login (Guardian) ──────────────────────"
    echo ""
    echo "  The Guardian monitors Genesis from this host machine."
    echo "  Basic health checks (ping, API, heartbeat) work without"
    echo "  Claude Code, but autonomous diagnosis — where Guardian"
    echo "  uses AI to investigate and fix problems — requires it."
    echo ""
    if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
        echo "  Since you're on a headless machine:"
        echo "    1. It will print a URL — open it in YOUR browser"
        echo "    2. Complete the OAuth flow in your browser"
        echo "    3. Come back here — Claude Code will detect the login"
    else
        echo "  A browser window will open for OAuth login."
    fi
    echo ""
    read -rp "  Log in now? [Y/n] " _cc_login
    if [ "${_cc_login:-Y}" != "n" ] && [ "${_cc_login:-Y}" != "N" ]; then
        if "$CLAUDE_PATH" login; then
            CC_AUTHENTICATED=true
            echo "  + Claude Code authenticated"
        else
            echo "  WARNING: Login failed or was skipped"
            echo "  To log in later: claude login (from this host)"
        fi
    else
        echo "  Skipped. To log in later: claude login (from this host)"
    fi
fi

# ── Step 2: Copy code from local repo ────────────────────────────────
# The user already has the repo (they cloned it to run this script).
# No need for a second git clone — just copy from the local checkout.

echo ""
echo "[2/14] Setting up code..."

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# If already running from the install dir (e.g. host-setup.sh cloned directly
# into INSTALL_DIR), skip the copy — code is already in place.
if [ "$(cd "$REPO_ROOT" && pwd)" = "$(cd "$INSTALL_DIR" 2>/dev/null && pwd)" ] 2>/dev/null; then
    echo "  Code already in $INSTALL_DIR (same as repo)"
elif [ -d "$INSTALL_DIR/src/genesis/guardian" ]; then
    echo "  Updating from local repo: $REPO_ROOT"
    # Use cp -rT to merge contents into existing dirs (not nest src/src/)
    cp -rT "$REPO_ROOT/src" "$INSTALL_DIR/src"
    cp -rT "$REPO_ROOT/config" "$INSTALL_DIR/config"
    cp -rT "$REPO_ROOT/scripts" "$INSTALL_DIR/scripts"
else
    echo "  Copying from local repo: $REPO_ROOT"
    mkdir -p "$INSTALL_DIR"
    cp -r "$REPO_ROOT/src" "$INSTALL_DIR/src"
    cp -r "$REPO_ROOT/config" "$INSTALL_DIR/config"
    cp -r "$REPO_ROOT/scripts" "$INSTALL_DIR/scripts"
fi

# ── Step 3: Create venv ──────────────────────────────────────────────

echo ""
echo "[3/14] Creating virtual environment..."

if [ ! -d "$VENV_DIR" ]; then
    $PYTHON -m venv "$VENV_DIR"
fi

# Debian creates venvs without pip even when ensurepip imports (ensurepip
# is present but non-functional on Debian). Bootstrap pip if missing.
if [ ! -f "$VENV_DIR/bin/pip" ]; then
    echo "  pip not in venv — bootstrapping..."
    "$VENV_DIR/bin/python" -m ensurepip --upgrade 2>/dev/null || \
        curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$VENV_DIR/bin/python"
fi

"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet pyyaml
echo "  venv: $VENV_DIR"

# ── Step 4: Create state & shared directories ───────────────────────

echo ""
echo "[4/14] Creating state & shared directories..."

mkdir -p "$STATE_DIR"
mkdir -p "$STATE_DIR/shared/briefing"   # Genesis writes, Guardian reads
mkdir -p "$STATE_DIR/shared/guardian"    # Guardian writes, Genesis reads (Phase 2)
echo "  State dir: $STATE_DIR"
echo "  Shared:    $STATE_DIR/shared/{briefing,guardian}"

# ── Step 5: Generate guardian.yaml with detected values ───────────────

echo ""
echo "[5/14] Generating guardian.yaml..."

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
echo "[6/14] Telegram credential bridge..."

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
echo "[7/14] Generating CLAUDE.md for diagnostic CC..."

# Single source of truth: config/guardian-claude.md
# The gateway update command also copies this file after every git pull.
if [ -f "$INSTALL_DIR/config/guardian-claude.md" ]; then
    cp "$INSTALL_DIR/config/guardian-claude.md" "$INSTALL_DIR/CLAUDE.md"

    # No network block is appended: shared host/container facts live in the
    # user-level ~/.claude/CLAUDE.md (D16). CLAUDE.md is also deliberately NOT
    # marked --skip-worktree — that wedges the gateway's `git pull` once upstream
    # touches the tracked CLAUDE.md ("local changes would be overwritten"). The
    # gateway regenerates CLAUDE.md via checkout+cp on every update instead.

    # D16: the diagnostic CC runs with cwd = cc.work_dir (see diagnosis.py), so the
    # Guardian CLAUDE.md must live there to be auto-loaded as project context — the
    # install-dir copy is never seen by that CC session. Symlink so it tracks every
    # regeneration of the install-dir CLAUDE.md (install + gateway update/redeploy).
    _gd_work_dir="/var/lib/guardian-snapshots/cc-sessions"   # = guardian.yaml cc.work_dir default
    if [ ! -d "$_gd_work_dir" ]; then
        if sudo -n true 2>/dev/null; then
            sudo mkdir -p "$_gd_work_dir" \
                && sudo chown "$(id -un):$(id -gn)" "$_gd_work_dir" "$(dirname "$_gd_work_dir")" 2>/dev/null || true
        else
            mkdir -p "$_gd_work_dir" 2>/dev/null || true
        fi
    fi
    if [ -d "$_gd_work_dir" ]; then
        ln -sf "$INSTALL_DIR/CLAUDE.md" "$_gd_work_dir/CLAUDE.md" \
            && echo "  + Guardian CLAUDE.md linked into work_dir ($_gd_work_dir)" \
            || echo "  WARN: could not link CLAUDE.md into $_gd_work_dir"
    else
        echo "  WARN: work_dir $_gd_work_dir absent — diagnostic CC won't load the Guardian CLAUDE.md"
    fi
    echo "  CLAUDE.md generated"
else
    echo "  WARNING: config/guardian-claude.md not found — CLAUDE.md not generated"
fi

# ── Step 8: Install systemd units ─────────────────────────────────────

echo ""
echo "[8/14] Installing systemd units..."

mkdir -p "$SYSTEMD_DIR"

for unit in genesis-guardian.service genesis-guardian.timer \
            genesis-guardian-watchman.service genesis-guardian-watchman.timer; do
    cp "$INSTALL_DIR/config/$unit" "$SYSTEMD_DIR/$unit"
    echo "  Installed $unit"
done

systemctl --user daemon-reload
echo "  Reloaded systemd"

# ── Step 9: Kernel OOM protection ─────────────────────────────────────
# Prevent hard VM freezes under memory pressure. Cloud VMs often have
# min_free_kbytes <100MB — not enough headroom for the OOM killer to act
# before the system enters a death spiral. Incident: 2026-05-15.

echo ""
echo "[9/14] Configuring kernel OOM protection..."

# Read host RAM from /proc/meminfo (host_mem_gb isn't set in this script)
_host_mem_kb=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}')
_host_mem_gb=$(( ${_host_mem_kb:-0} / 1048576 ))

if [ "$_host_mem_gb" -gt 0 ] 2>/dev/null; then
    # Scale min_free_kbytes to ~1% of host RAM (floor 128MB, cap 1024MB)
    _min_free_mb=$(( _host_mem_gb * 10 ))  # ~1% in MB
    if [ "$_min_free_mb" -lt 128 ]; then _min_free_mb=128; fi
    if [ "$_min_free_mb" -gt 1024 ]; then _min_free_mb=1024; fi
    _min_free_kb=$(( _min_free_mb * 1024 ))

    # Idempotency: don't downgrade if someone already set a higher value.
    _current_min_free=$(sysctl -n vm.min_free_kbytes 2>/dev/null || echo "0")
    if [ "${_current_min_free:-0}" -ge "$_min_free_kb" ]; then
        echo "  OOM tuning already adequate (min_free_kbytes=${_current_min_free})"
    elif sudo -n true 2>/dev/null; then
        # Best-effort tuning: guard each sudo command so a failure can't abort
        # the install under `set -euo pipefail` (the WARN branch below is the
        # intended fallback when tuning can't be applied).
        sudo tee /etc/sysctl.d/99-genesis-oom-tuning.conf > /dev/null << SYSCTL || true
# Genesis — kernel OOM protection (installed by install_guardian.sh)
# Prevents VM freeze under memory pressure. Safe to customize.
vm.min_free_kbytes = $_min_free_kb
vm.watermark_scale_factor = 50
vm.oom_kill_allocating_task = 1
SYSCTL
        sudo sysctl --system > /dev/null 2>&1 || true
        echo "  + OOM tuning applied (min_free=${_min_free_mb}MB for ${_host_mem_gb}GB host)"
    else
        echo "  WARN  Cannot apply OOM tuning (no passwordless sudo)"
        echo "        To apply manually:"
        echo "        echo 'vm.min_free_kbytes = $_min_free_kb' | sudo tee /etc/sysctl.d/99-genesis-oom-tuning.conf"
        echo "        echo 'vm.watermark_scale_factor = 50' | sudo tee -a /etc/sysctl.d/99-genesis-oom-tuning.conf"
        echo "        echo 'vm.oom_kill_allocating_task = 1' | sudo tee -a /etc/sysctl.d/99-genesis-oom-tuning.conf"
        echo "        sudo sysctl --system"
    fi
else
    echo "  WARN  Cannot determine host RAM — skipping OOM tuning"
fi

# ── Step 9b: I/O tuning & BFQ scheduler ─────────────────────────────────
# Post-incident hardening (2026-05-25): reduce dirty page cache pressure
# and enable the BFQ I/O scheduler for fairer queue discipline.
# Reference configs: config/99-container-host.conf, config/60-ioscheduler.rules

if sudo -n true 2>/dev/null; then
    # Best-effort tuning: guard each sudo command so a failure can't abort the
    # install under `set -euo pipefail` (the SKIP branch below is the fallback).
    # I/O sysctl — always overwrite to pick up value changes on update
    sudo tee /etc/sysctl.d/99-genesis-io-tuning.conf > /dev/null << 'SYSCTL' || true
# Genesis — I/O pressure reduction (installed by install_guardian.sh)
# Reduces dirty page cache to prevent I/O death spirals under sustained write load.
vm.swappiness = 10
vm.dirty_ratio = 10
vm.dirty_background_ratio = 3
vm.vfs_cache_pressure = 50
SYSCTL
    sudo sysctl --system > /dev/null 2>&1 || true
    echo "  + I/O tuning applied (swappiness=10, dirty_ratio=10)"

    # BFQ I/O scheduler — always refresh from repo
    if [ -d "$INSTALL_DIR/config" ] && [ -f "$INSTALL_DIR/config/60-ioscheduler.rules" ]; then
        sudo cp "$INSTALL_DIR/config/60-ioscheduler.rules" /etc/udev/rules.d/ || true
        sudo udevadm control --reload-rules 2>/dev/null || true
        echo "  + BFQ I/O scheduler rule installed"
    fi
else
    echo "  SKIP  I/O tuning (no passwordless sudo). See config/99-container-host.conf"
fi

# ── Step 10: Install gateway script ────────────────────────────────────

echo ""
echo "[10/14] Installing guardian gateway..."

mkdir -p "$HOME/.local/bin"
cp "$INSTALL_DIR/scripts/guardian-gateway.sh" "$HOME/.local/bin/guardian-gateway.sh"
chmod +x "$HOME/.local/bin/guardian-gateway.sh"
echo "  Installed guardian-gateway.sh"

# ── Step 11: Set up bidirectional SSH ────────────────────────────────
# Generate a dedicated SSH key in the container, then install a
# command-restricted authorized_keys entry on this host so Genesis
# can call the gateway script (restart-timer, pause, resume, status).

echo ""
echo "[11/14] Setting up bidirectional SSH..."

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
    GUARD_FROM_IP=""
    GUARD_BASE_OPTS="command=\"$HOME/.local/bin/guardian-gateway.sh\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty"
    # Treat the key as installed only if it is present AND hardened (no-pty is
    # the marker for this installer version's options). A key present without
    # no-pty is an older, under-hardened entry — re-install to upgrade it.
    if grep -F "$PUBKEY_BLOB" "$HOME/.ssh/authorized_keys" 2>/dev/null | grep -q 'no-pty'; then
        echo "  Authorized key already installed and hardened"
    else
        # Remove any existing control entry (stale key OR older un-hardened one)
        if grep -q "genesis-guardian-control" "$HOME/.ssh/authorized_keys" 2>/dev/null; then
            sed -i '/genesis-guardian-control/d' "$HOME/.ssh/authorized_keys"
            echo "  Removed prior guardian key entry (re-installing hardened)"
        fi
        mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
        # Harden beyond the ForceCommand:
        #   no-pty  — the gateway is a non-interactive JSON contract, never a shell.
        #   from="" — bind the key to the container's source IP so a stolen key
        #             can't be used from any other LAN host. Derive that IP from
        #             the container's ACTUAL egress route to the host, NOT
        #             `incus list -c4` (which can return the tailscale address the
        #             host never sees). If it can't be derived, install without
        #             from= (no-pty still applies) rather than guess and risk a
        #             lockout; a wrong from= is caught + rolled back by the
        #             connectivity test below.
        GUARD_FROM_IP=$(incus exec "$CONTAINER_NAME" -- ip -4 route get "${TS_IP:-}" 2>/dev/null | grep -oP 'src \K\S+' | head -1 || true)
        if [ -n "$GUARD_FROM_IP" ]; then
            echo "from=\"${GUARD_FROM_IP}\",${GUARD_BASE_OPTS} ${PUBKEY}" >> "$HOME/.ssh/authorized_keys"
        else
            echo "  NOTE: could not derive container source IP — installing without from= (no-pty still applied)"
            echo "${GUARD_BASE_OPTS} ${PUBKEY}" >> "$HOME/.ssh/authorized_keys"
        fi
        chmod 600 "$HOME/.ssh/authorized_keys"
        echo "  Installed hardened command-restricted SSH key for Genesis→Guardian control"
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
    _guardian_ssh_test() {
        incus exec "$CONTAINER_NAME" -- su - "$CONTAINER_USER" -c \
            "ssh -i ${GUARDIAN_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=5 ${HOST_USER}@${HOST_IP} ping 2>&1" || echo "FAILED"
    }
    SSH_TEST=$(_guardian_ssh_test)
    if echo "$SSH_TEST" | grep -q '"ok": true'; then
        echo "  SSH bidirectional link verified${GUARD_FROM_IP:+ (source-restricted to ${GUARD_FROM_IP})}"
    elif [ -n "${GUARD_FROM_IP:-}" ]; then
        # A wrong from= restriction would silently lock Guardian out of the host.
        # Never let that persist: drop from= (keep no-pty + the other options)
        # and re-test. Guardian keeps working; the LAN source-restriction just
        # isn't applied until the correct source IP is determined.
        echo "  SSH test failed with from=\"${GUARD_FROM_IP}\" — retrying without from= (keeping no-pty)..."
        sed -i '/genesis-guardian-control/d' "$HOME/.ssh/authorized_keys"
        echo "${GUARD_BASE_OPTS} ${PUBKEY}" >> "$HOME/.ssh/authorized_keys"
        chmod 600 "$HOME/.ssh/authorized_keys"
        SSH_TEST=$(_guardian_ssh_test)
        if echo "$SSH_TEST" | grep -q '"ok": true'; then
            echo "  SSH link verified WITHOUT from= — the derived source IP (${GUARD_FROM_IP}) is not what the host observes."
            echo "  Guardian works; the LAN key-theft restriction is NOT enforced. To enable it, add"
            echo "  from=\"<container's host-facing source IP>\" to the guardian key in ~/.ssh/authorized_keys."
        else
            echo ""
            echo "  WARNING: SSH test still failing after dropping from=. Guardian bidirectional monitoring may not work."
            echo "  Error: $SSH_TEST"
            echo "  To fix manually, add this to ~/.ssh/authorized_keys on the host:"
            echo "    ${GUARD_BASE_OPTS} ${PUBKEY}"
            echo ""
        fi
    else
        echo ""
        echo "  WARNING: SSH test failed. Guardian bidirectional monitoring may not work."
        echo "  Error: $SSH_TEST"
        echo ""
        echo "  To fix manually, add this to ~/.ssh/authorized_keys on the host:"
        echo "    ${GUARD_BASE_OPTS} ${PUBKEY}"
        echo ""
    fi
fi

# ── Step 12: Configure shared filesystem mounts ─────────────────────
# Incus disk devices give Genesis and Guardian a shared data plane.
# Genesis writes briefings; Guardian reads them before CC diagnosis.
# Uses shift=true for transparent UID mapping (requires kernel 5.12+).

echo ""
echo "[12/14] Configuring shared filesystem mounts..."

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

# ── Step 13: Enable timers ────────────────────────────────────────────

echo ""
echo "[13/14] Enabling timers..."

systemctl --user enable genesis-guardian.timer
systemctl --user start genesis-guardian.timer
echo "  Guardian timer: enabled + started"

systemctl --user enable genesis-guardian-watchman.timer
systemctl --user start genesis-guardian-watchman.timer
echo "  Watchman timer: enabled + started"

# ── Step 14: Verification ────────────────────────────────────────────

echo ""
echo "[14/14] Running verification..."

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
