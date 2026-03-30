#!/bin/bash
# Genesis — Host VM Setup
# Creates an Incus (LXC) system container with Genesis pre-configured.
# Run this on the bare host VM (e.g. fresh Proxmox/Ubuntu machine).
#
# Usage:
#   ./scripts/host-setup.sh [options]
#
# Options:
#   --container-name NAME  Container name (default: genesis)
#   --ram SIZE             Memory limit (default: 24GiB)
#   --disk SIZE            Disk size (default: 30GB)
#   --cpus N               CPU limit (default: 8)
#   --repo URL             Genesis git repo URL (default: current remote)
#   --branch NAME          Branch to clone (default: main)
#   --non-interactive      Skip all prompts
#   -h, --help             Show this help

set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────
CONTAINER_NAME="genesis"
RAM="24GiB"
DISK="30GB"
CPUS="8"
REPO_URL=""
BRANCH="main"
NON_INTERACTIVE=0

# ── Parse args ───────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --container-name) [ $# -ge 2 ] || { echo "ERROR: $1 requires a value"; exit 1; }; CONTAINER_NAME="$2"; shift ;;
        --ram)            [ $# -ge 2 ] || { echo "ERROR: $1 requires a value"; exit 1; }; RAM="$2"; shift ;;
        --disk)           [ $# -ge 2 ] || { echo "ERROR: $1 requires a value"; exit 1; }; DISK="$2"; shift ;;
        --cpus)           [ $# -ge 2 ] || { echo "ERROR: $1 requires a value"; exit 1; }; CPUS="$2"; shift ;;
        --repo)           [ $# -ge 2 ] || { echo "ERROR: $1 requires a value"; exit 1; }; REPO_URL="$2"; shift ;;
        --branch)         [ $# -ge 2 ] || { echo "ERROR: $1 requires a value"; exit 1; }; BRANCH="$2"; shift ;;
        --non-interactive) NON_INTERACTIVE=1 ;;
        -h|--help)
            sed -n '2,/^$/{ s/^# \?//; p }' "$0"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

# Auto-detect repo URL from current remote if not specified
if [ -z "$REPO_URL" ]; then
    REPO_URL=$(git remote get-url origin 2>/dev/null || echo "")
    if [ -z "$REPO_URL" ]; then
        echo "ERROR: No --repo specified and not in a git repo with a remote."
        echo "Usage: $0 --repo https://github.com/user/genesis.git"
        exit 1
    fi
fi

echo ""
echo "  Genesis — Host VM Setup"
echo "  ─────────────────────────────────────────"
echo "  Container: $CONTAINER_NAME"
echo "  RAM: $RAM | Disk: $DISK | CPUs: $CPUS"
echo "  Repo: $REPO_URL ($BRANCH)"
echo ""

# ── Pre-flight (host level) ─────────────────────────────────
echo "  Pre-flight checks..."
PREFLIGHT_OK=1

# Root or sudo
if [ "$(id -u)" != "0" ] && ! sudo -n true 2>/dev/null; then
    echo "    FAIL  Need root or passwordless sudo"
    PREFLIGHT_OK=0
else
    echo "    OK    Root/sudo access"
fi

# OS check
if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo "    OK    OS: $PRETTY_NAME"
else
    echo "    WARN  Cannot determine OS (continuing anyway)"
fi

# Disk space on host
host_avail_kb=$(df --output=avail /home 2>/dev/null | tail -1 | tr -d ' ' || echo "0")
host_avail_h=$(df -h /home 2>/dev/null | tail -1 | awk '{print $4}')
if [ "$host_avail_kb" -lt 5242880 ] 2>/dev/null; then
    echo "    FAIL  Need >= 5GB free on /home, only $host_avail_h available"
    PREFLIGHT_OK=0
else
    echo "    OK    Disk: $host_avail_h free"
fi

# RAM
host_mem_kb=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}')
host_mem_gb=$((${host_mem_kb:-0} / 1048576))
if [ "${host_mem_kb:-0}" -lt 8388608 ] 2>/dev/null; then
    echo "    FAIL  Need >= 8GB RAM on host, found ${host_mem_gb}GB"
    PREFLIGHT_OK=0
else
    echo "    OK    RAM: ${host_mem_gb}GB"
fi

# Network
if curl --max-time 5 -sf "https://github.com" >/dev/null 2>&1; then
    echo "    OK    Network: github.com reachable"
else
    echo "    FAIL  Cannot reach github.com"
    PREFLIGHT_OK=0
fi

if [ "$PREFLIGHT_OK" = "0" ]; then
    echo ""
    echo "  Pre-flight FAILED — fix the errors above."
    exit 1
fi
echo ""

# ── Install Incus ────────────────────────────────────────────
if command -v incus &>/dev/null; then
    echo "  Incus already installed ($(incus version 2>/dev/null || echo 'unknown'))"
else
    echo "  Installing Incus..."
    if command -v snap &>/dev/null; then
        sudo snap install incus --channel=latest/stable
    elif command -v apt-get &>/dev/null; then
        # Debian/Ubuntu without snap — use Zabbly repo
        sudo mkdir -p /etc/apt/keyrings/
        curl -fsSL https://pkgs.zabbly.com/key.asc | sudo gpg --yes --dearmor -o /etc/apt/keyrings/zabbly.gpg
        echo "deb [signed-by=/etc/apt/keyrings/zabbly.gpg] https://pkgs.zabbly.com/incus/stable $(. /etc/os-release && echo "$VERSION_CODENAME") main" | \
            sudo tee /etc/apt/sources.list.d/zabbly-incus.list
        sudo apt-get update -qq
        sudo apt-get install -y incus
    else
        echo "  ERROR: Cannot install Incus — no snap or apt-get available."
        echo "  Install manually: https://linuxcontainers.org/incus/docs/main/installing/"
        exit 1
    fi
    echo "  + Incus installed"

    echo "  Initializing Incus (minimal)..."
    sudo incus admin init --minimal
    echo "  + Incus initialized"

    # Add current user to incus-admin group if not root
    if [ "$(id -u)" != "0" ]; then
        sudo usermod -aG incus-admin "$(whoami)" 2>/dev/null || true
        echo "  + Added $(whoami) to incus-admin group"
        echo "  NOTE: You may need to log out and back in for group membership to take effect."
        echo "        If the next step fails, run: newgrp incus-admin"
    fi
fi

# ── Create container ─────────────────────────────────────────
if incus info "$CONTAINER_NAME" &>/dev/null; then
    echo ""
    echo "  Container '$CONTAINER_NAME' already exists."
    if [ "$NON_INTERACTIVE" = "1" ]; then
        echo "  Continuing with existing container."
    else
        read -rp "  Delete and recreate? [y/N] " _recreate
        if [ "${_recreate:-N}" = "y" ] || [ "${_recreate:-N}" = "Y" ]; then
            incus delete "$CONTAINER_NAME" --force
            echo "  + Old container deleted"
        else
            echo "  Continuing with existing container."
        fi
    fi
fi

if ! incus info "$CONTAINER_NAME" &>/dev/null; then
    echo "  Creating container '$CONTAINER_NAME'..."
    # images:ubuntu/noble — the images: remote is always available after incus admin init
    incus launch images:ubuntu/noble "$CONTAINER_NAME"
    echo "  + Container created"

    # Apply resource limits
    incus config set "$CONTAINER_NAME" limits.memory "$RAM"
    incus config set "$CONTAINER_NAME" limits.cpu "$CPUS"
    echo "  + Resource limits: RAM=$RAM, CPUs=$CPUS"

    # Disk and IOPS limits
    incus config device set "$CONTAINER_NAME" root size "$DISK" 2>/dev/null || true
    incus config device set "$CONTAINER_NAME" root limits.read 190MB 2>/dev/null || true
    incus config device set "$CONTAINER_NAME" root limits.write 90MB 2>/dev/null || true
    echo "  + Disk: $DISK, IOPS limits applied"

    # Wait for container to be ready
    echo "  Waiting for container to initialize..."
    incus exec "$CONTAINER_NAME" -- cloud-init status --wait 2>/dev/null || sleep 10
    echo "  + Container ready"
fi

# ── Set up user inside container ─────────────────────────────
echo "  Setting up user inside container..."

# Create ubuntu user if it doesn't exist
incus exec "$CONTAINER_NAME" -- bash -c 'id ubuntu &>/dev/null || useradd -m -s /bin/bash ubuntu' 2>/dev/null

# Enable linger for systemd user services
incus exec "$CONTAINER_NAME" -- loginctl enable-linger ubuntu 2>/dev/null || true

# Install git inside container (needed for clone)
incus exec "$CONTAINER_NAME" -- bash -c 'command -v git &>/dev/null || { apt-get update -qq && apt-get install -y -qq git; }' 2>/dev/null
echo "  + User 'ubuntu' configured"

# Resolve actual UID (don't assume 1000)
UBUNTU_UID=$(incus exec "$CONTAINER_NAME" -- id -u ubuntu 2>/dev/null || echo "1000")

# ── Clone Genesis inside container ───────────────────────────
echo "  Cloning Genesis into container..."

# Check if repo needs auth (private repo)
if echo "$REPO_URL" | grep -q "github.com" && ! curl -sf "$REPO_URL" >/dev/null 2>&1; then
    echo "  NOTE: Private repo detected. You may need to configure git credentials"
    echo "        inside the container after this script finishes:"
    echo "        incus exec $CONTAINER_NAME --user $UBUNTU_UID -t -- bash"
fi

incus exec "$CONTAINER_NAME" --user "$UBUNTU_UID" \
    --env "HOME=/home/ubuntu" -- bash -c "
    if [ -d ${HOME}/genesis ]; then
        echo '    . Genesis repo already exists'
        cd ${HOME}/genesis && git pull --ff-only 2>/dev/null || true
    else
        GIT_TERMINAL_PROMPT=0 git clone --branch '$BRANCH' '$REPO_URL' ${HOME}/genesis 2>&1 | tail -3
    fi
" || {
    echo "  WARNING: Git clone failed (private repo?)"
    echo "  You can push the code manually:"
    echo "    incus file push -r . ${CONTAINER_NAME}${HOME}/genesis/"
    echo "  Then run install.sh inside the container."
}

# ── Run install.sh inside container ──────────────────────────
# Guard: only run if the repo actually exists
if incus exec "$CONTAINER_NAME" --user "$UBUNTU_UID" -- test -f ${HOME}/genesis/scripts/install.sh; then
    echo ""
    echo "  Running install.sh inside container..."
    echo "  ─────────────────────────────────────────"
    echo ""

    _install_flags=""
    [ "$NON_INTERACTIVE" = "1" ] && _install_flags="--non-interactive"

    # shellcheck disable=SC2086  # Intentional: empty string should vanish
    incus exec "$CONTAINER_NAME" --user "$UBUNTU_UID" -t --cwd ${HOME}/genesis -- \
        bash scripts/install.sh $_install_flags || {
        echo ""
        echo "  WARNING: install.sh exited with errors."
        echo "  Connect to the container to debug:"
        echo "    incus exec $CONTAINER_NAME --user $UBUNTU_UID -t -- bash"
    }
else
    echo ""
    echo "  ERROR: Genesis repo not found in container."
    echo "  Push the code manually, then run install.sh:"
    echo "    incus file push -r . ${CONTAINER_NAME}${HOME}/genesis/"
    echo "    incus exec $CONTAINER_NAME --user $UBUNTU_UID -t --cwd ${HOME}/genesis -- bash scripts/install.sh"
fi

# ── Install Guardian on host ───────────────────────────────────
CONTAINER_IP=$(incus list "$CONTAINER_NAME" -f csv -c 4 2>/dev/null | grep -oP '\d+\.\d+\.\d+\.\d+' | head -1 || echo "unknown")

echo ""
echo "  Installing Guardian (host-side health monitor)..."
echo "  ─────────────────────────────────────────"
echo ""

# Guardian install script is in the Genesis repo (already cloned to INSTALL_DIR or container)
_guardian_script="$HOME/.local/share/genesis-guardian/scripts/install_guardian.sh"
if [ -f "$_guardian_script" ]; then
    _guardian_flags="--container-name $CONTAINER_NAME"
    [ -n "$REPO_URL" ] && _guardian_flags="$_guardian_flags --repo-url $REPO_URL"
    [ "$NON_INTERACTIVE" = "1" ] && _guardian_flags="$_guardian_flags --non-interactive"
    # shellcheck disable=SC2086
    bash "$_guardian_script" $_guardian_flags || {
        echo "  WARNING: Guardian installation failed."
        echo "  Run manually later: bash scripts/install_guardian.sh --container-name $CONTAINER_NAME"
    }
else
    # Guardian not yet cloned — clone it then install
    _GUARDIAN_DIR="$HOME/.local/share/genesis-guardian"
    if [ ! -d "$_GUARDIAN_DIR/.git" ]; then
        echo "  Cloning Genesis repo for Guardian..."
        git clone "$REPO_URL" "$_GUARDIAN_DIR" 2>&1 | tail -3 || true
    fi
    if [ -f "$_GUARDIAN_DIR/scripts/install_guardian.sh" ]; then
        _guardian_flags="--container-name $CONTAINER_NAME"
        [ -n "$REPO_URL" ] && _guardian_flags="$_guardian_flags --repo-url $REPO_URL"
        [ "$NON_INTERACTIVE" = "1" ] && _guardian_flags="$_guardian_flags --non-interactive"
        # shellcheck disable=SC2086
        bash "$_GUARDIAN_DIR/scripts/install_guardian.sh" $_guardian_flags || {
            echo "  WARNING: Guardian installation failed."
            echo "  Run manually later: bash $_GUARDIAN_DIR/scripts/install_guardian.sh --container-name $CONTAINER_NAME"
        }
    else
        echo "  WARNING: Guardian install script not found. Install manually later."
    fi
fi

# ── Report ───────────────────────────────────────────────────

echo ""
echo "  ─────────────────────────────────────────"
echo "  Genesis installed in container '$CONTAINER_NAME'"
echo ""
echo "  Container IP: $CONTAINER_IP"
echo "  Access:       incus exec $CONTAINER_NAME --user $UBUNTU_UID -t -- bash"
echo "  Web UI:       http://$CONTAINER_IP:5000"
echo "  SSH tunnel:   ssh -L 5000:$CONTAINER_IP:5000 $(whoami)@$(hostname)"
echo ""
echo "  Guardian:     systemctl --user status genesis-guardian.timer"
echo ""
echo "  Manage:"
echo "    incus start|stop|restart $CONTAINER_NAME"
echo "    incus exec $CONTAINER_NAME --user $UBUNTU_UID -- systemctl --user status agent-zero"
echo ""
