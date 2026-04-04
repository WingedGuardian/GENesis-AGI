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
    if command -v snap &>/dev/null && sudo snap install incus --channel=latest/stable 2>/dev/null; then
        true  # snap install succeeded
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

    # Add current user to incus-admin group if not root.
    # The re-exec below handles activating the group immediately.
    if [ "$(id -u)" != "0" ]; then
        sudo usermod -aG incus-admin "$(whoami)" 2>/dev/null || true
        echo "  + Added $(whoami) to incus-admin group"
    fi
fi

# ── Activate incus-admin group (re-exec if needed) ──────────
# After fresh Incus install, the current shell doesn't have incus-admin yet.
# Rather than asking the user to log out/in or run newgrp manually, re-exec
# the entire script under `sg incus-admin` which activates the group immediately.
if [ "$(id -u)" != "0" ] && ! id -Gn | grep -qw "incus-admin"; then
    echo "  Activating incus-admin group (re-running script)..."
    exec sg incus-admin -c "$(realpath "$0") $*"
fi

# ── Ensure IP forwarding and bridge NAT ──────────────────────
# Cloud VMs (GCP, AWS, Azure) often have IP forwarding disabled and
# `incus admin init --minimal` may not enable NAT on the bridge.
# Without both, containers have zero outbound connectivity.
if [ "$(sysctl -n net.ipv4.ip_forward 2>/dev/null)" != "1" ]; then
    echo "  Enabling IP forwarding..."
    sudo sysctl -w net.ipv4.ip_forward=1 >/dev/null
    echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/99-incus-forward.conf >/dev/null
    echo "  + IP forwarding enabled"
fi

# Find the managed bridge (not the host NIC). Column order: NAME,TYPE,MANAGED,...
# Filter for MANAGED=YES to avoid trying to modify physical interfaces like ens4.
_INCUS_BRIDGE=$(incus network list --format csv 2>/dev/null | grep ",YES," | head -1 | cut -d, -f1)
if [ -n "$_INCUS_BRIDGE" ]; then
    _NAT_STATUS=$(incus network get "$_INCUS_BRIDGE" ipv4.nat 2>/dev/null || echo "")
    if [ "$_NAT_STATUS" != "true" ]; then
        echo "  Enabling NAT on bridge $_INCUS_BRIDGE..."
        incus network set "$_INCUS_BRIDGE" ipv4.nat true
        echo "  + Bridge NAT enabled"
    fi
fi

# ── Firewall: ensure Incus bridge traffic is allowed ────────
# UFW with FORWARD DROP blocks DHCP/NAT between the container and
# the host bridge. Incus snap handles this automatically; the apt
# package does not.
_INCUS_BRIDGE=$(incus network list --format csv 2>/dev/null | grep ",YES," | head -1 | cut -d, -f1)
if [ -n "$_INCUS_BRIDGE" ] && command -v ufw &>/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
    if ! sudo ufw status | grep -q "on $_INCUS_BRIDGE"; then
        echo "  UFW detected — allowing traffic on $_INCUS_BRIDGE..."
        sudo ufw allow in on "$_INCUS_BRIDGE" >/dev/null 2>&1
        sudo ufw allow out on "$_INCUS_BRIDGE" >/dev/null 2>&1
        sudo ufw route allow in on "$_INCUS_BRIDGE" >/dev/null 2>&1
        sudo ufw route allow out on "$_INCUS_BRIDGE" >/dev/null 2>&1
        echo "  + UFW rules added for Incus bridge"
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

    # Disk: override the root device to actually resize the filesystem.
    # On LVM-backed pools, the default thin volume is only 10GB.
    # "device override" resizes the underlying LV + filesystem.
    incus config device override "$CONTAINER_NAME" root size="$DISK" 2>/dev/null || true
    # I/O limits
    incus config device set "$CONTAINER_NAME" root limits.read 190MB 2>/dev/null || true
    incus config device set "$CONTAINER_NAME" root limits.write 90MB 2>/dev/null || true
    echo "  + Disk: $DISK, IOPS limits applied"

    # Wait for container to be ready
    echo "  Waiting for container to initialize..."
    incus exec "$CONTAINER_NAME" -- cloud-init status --wait 2>/dev/null || sleep 10
    echo "  + Container ready"
fi

# ── Verify container networking ──────────────────────────────
# Some container images (images:ubuntu/noble) default to IPv6-only via netplan.
# Without DHCPv4, the container has no IPv4 address and can't reach IPv4 hosts.
echo "  Checking container networking..."
_CONTAINER_IP=$(incus list "$CONTAINER_NAME" -f csv -c 4 2>/dev/null | grep -oP '\d+\.\d+\.\d+\.\d+' | head -1 || true)
if [ -z "$_CONTAINER_IP" ]; then
    echo "  No IPv4 address — enabling DHCPv4 via netplan..."
    incus exec "$CONTAINER_NAME" -- bash -c '
        mkdir -p /etc/netplan
        cat > /etc/netplan/10-dhcp4.yaml <<NETPLAN
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: true
NETPLAN
        netplan apply 2>/dev/null
    '
    sleep 5
    _CONTAINER_IP=$(incus list "$CONTAINER_NAME" -f csv -c 4 2>/dev/null | grep -oP '\d+\.\d+\.\d+\.\d+' | head -1 || true)
    if [ -n "$_CONTAINER_IP" ]; then
        echo "  + IPv4 address acquired: $_CONTAINER_IP"
    else
        echo "  ERROR: Container still has no IPv4 address after enabling DHCPv4."
        echo "  Debug: incus exec $CONTAINER_NAME -- networkctl status eth0"
        echo "         incus network show incusbr0"
        exit 1
    fi
else
    echo "  + Container networking OK ($_CONTAINER_IP)"
fi

# DNS fallback: cloud VMs may have DNS resolvers the container can't reach.
if ! incus exec "$CONTAINER_NAME" -- nslookup archive.ubuntu.com &>/dev/null 2>&1; then
    echo "  DNS not resolving — injecting Google DNS fallback..."
    incus exec "$CONTAINER_NAME" -- bash -c '
        cat > /etc/resolv.conf <<RESOLV
nameserver 8.8.8.8
nameserver 8.8.4.4
RESOLV
    '
    if incus exec "$CONTAINER_NAME" -- nslookup archive.ubuntu.com &>/dev/null 2>&1; then
        echo "  + DNS fixed (using Google DNS)"
    else
        echo "  WARNING: DNS still not working after fallback."
        echo "  Check: incus exec $CONTAINER_NAME -- ping -c1 8.8.8.8"
    fi
fi

# ── Set up user inside container ─────────────────────────────
echo "  Setting up user inside container..."

# Create ubuntu user if it doesn't exist
incus exec "$CONTAINER_NAME" -- bash -c 'id ubuntu &>/dev/null || useradd -m -s /bin/bash ubuntu' 2>/dev/null

# Enable linger for systemd user services
incus exec "$CONTAINER_NAME" -- loginctl enable-linger ubuntu 2>/dev/null || true

# Install all prerequisites inside container as root.
# install.sh has its own checks as defense-in-depth, but pre-installing here
# (as root, no sudo needed) is more reliable than install.sh's sudo fallbacks.
echo "  Installing prerequisites in container (1-3 min, please wait)..."
incus exec "$CONTAINER_NAME" -- bash -c '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -q \
        git curl sudo \
        python3 python3-pip python3.12-venv \
        nodejs npm 2>&1 | grep -E "^(Get:|Unpacking|Setting up)" | head -30 || true
'
echo "  + Prerequisites installed"

# Resolve actual UID (don't assume 1000)
UBUNTU_UID=$(incus exec "$CONTAINER_NAME" -- id -u ubuntu 2>/dev/null || echo "1000")

# ── Clone Genesis inside container ───────────────────────────
echo "  Cloning Genesis into container..."

# Check if repo needs auth (private repo)
if echo "$REPO_URL" | grep -q "github.com" && ! curl -sf "$REPO_URL" >/dev/null 2>&1; then
    echo "  NOTE: Private repo detected. You may need to configure git credentials"
    echo "        inside the container after this script finishes:"
    echo "        incus exec $CONTAINER_NAME --user $UBUNTU_UID --env HOME=/home/ubuntu --env XDG_RUNTIME_DIR=/run/user/$UBUNTU_UID --cwd /home/ubuntu/genesis -t -- bash -l"
fi

# NOTE: The bash -c uses single quotes so /home/ubuntu paths survive the
# prepare-public-release.sh sed replacement (which converts /home/ubuntu/ to
# ${HOME}/ — that would break in double-quoted strings on a non-ubuntu host).
# BRANCH and REPO_URL are injected via --env instead.
incus exec "$CONTAINER_NAME" --user "$UBUNTU_UID" \
    --env "HOME=/home/ubuntu" \
    --env "_BRANCH=$BRANCH" --env "_REPO_URL=$REPO_URL" -- \
    bash -c '
    _DEST=/home/ubuntu/genesis
    if [ -d "$_DEST" ]; then
        echo "    . Genesis repo already exists"
        cd "$_DEST" && git pull --ff-only 2>/dev/null || true
    else
        GIT_TERMINAL_PROMPT=0 git clone --branch "$_BRANCH" "$_REPO_URL" "$_DEST" 2>&1 | tail -3
    fi
' || {
    echo "  WARNING: Git clone failed (private repo?)"
    echo "  You can push the code manually:"
    echo "    incus file push -r . ${CONTAINER_NAME}/home/ubuntu/genesis/"
    echo "  Then run install.sh inside the container."
}

# ── Run install.sh inside container ──────────────────────────
# Guard: only run if the repo actually exists
if incus exec "$CONTAINER_NAME" --user "$UBUNTU_UID" --env "HOME=/home/ubuntu" -- test -f /home/ubuntu/genesis/scripts/install.sh; then
    echo ""
    echo "  Running install.sh inside container..."
    echo "  ─────────────────────────────────────────"
    echo ""

    _install_flags=""
    [ "$NON_INTERACTIVE" = "1" ] && _install_flags="--non-interactive"

    # shellcheck disable=SC2086  # Intentional: empty string should vanish
    incus exec "$CONTAINER_NAME" --user "$UBUNTU_UID" \
        --env "HOME=/home/ubuntu" \
        --env "XDG_RUNTIME_DIR=/run/user/$UBUNTU_UID" \
        -t --cwd /home/ubuntu/genesis -- \
        bash scripts/install.sh $_install_flags || {
        echo ""
        echo "  WARNING: install.sh exited with errors."
        echo "  Connect to the container to debug:"
        echo "    incus exec $CONTAINER_NAME --user $UBUNTU_UID --env HOME=/home/ubuntu --env XDG_RUNTIME_DIR=/run/user/$UBUNTU_UID --cwd /home/ubuntu/genesis -t -- bash -l"
    }
else
    echo ""
    echo "  ERROR: Genesis repo not found in container."
    echo "  Push the code manually, then run install.sh:"
    echo "    incus file push -r . ${CONTAINER_NAME}/home/ubuntu/genesis/"
    echo "    incus exec $CONTAINER_NAME --user $UBUNTU_UID --env HOME=/home/ubuntu --env XDG_RUNTIME_DIR=/run/user/$UBUNTU_UID -t --cwd /home/ubuntu/genesis -- bash scripts/install.sh"
fi

# ── Install Guardian on host ───────────────────────────────────
CONTAINER_IP=$(incus list "$CONTAINER_NAME" -f csv -c 4 2>/dev/null | grep -oP '\d+\.\d+\.\d+\.\d+' | head -1 || echo "unknown")

echo ""
echo "  Installing Guardian (host-side health monitor)..."
echo "  ─────────────────────────────────────────"
echo ""

# Run install_guardian.sh from the LOCAL checkout (this repo).
# install_guardian.sh copies code from its parent dir into ~/.local/share/genesis-guardian.
_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
_guardian_script="$_SCRIPT_DIR/install_guardian.sh"
if [ -f "$_guardian_script" ]; then
    _guardian_flags="--container-name $CONTAINER_NAME"
    [ "$NON_INTERACTIVE" = "1" ] && _guardian_flags="$_guardian_flags --non-interactive"
    # Guardian uses systemctl --user, which needs the invoking user's session
    # bus — not root's. When run via sudo, drop back to the original user.
    # shellcheck disable=SC2086
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
        _guardian_uid=$(id -u "$SUDO_USER")
        sudo -u "$SUDO_USER" \
            XDG_RUNTIME_DIR="/run/user/$_guardian_uid" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$_guardian_uid/bus" \
            bash "$_guardian_script" $_guardian_flags || {
            echo "  WARNING: Guardian installation failed."
            echo "  Run manually later: bash $_guardian_script --container-name $CONTAINER_NAME"
        }
    else
        bash "$_guardian_script" $_guardian_flags || {
            echo "  WARNING: Guardian installation failed."
            echo "  Run manually later: bash $_guardian_script --container-name $CONTAINER_NAME"
        }
    fi
else
    echo "  WARNING: Guardian install script not found at $_guardian_script"
fi

# ── Report ───────────────────────────────────────────────────

echo ""
echo "  ─────────────────────────────────────────"
echo "  Genesis is ready."
echo "  ─────────────────────────────────────────"
echo ""
echo "  What to do next:"
echo ""
echo "    1. Connect to the Genesis container:"
echo ""
echo "       incus exec $CONTAINER_NAME --user $UBUNTU_UID --env HOME=/home/ubuntu --env XDG_RUNTIME_DIR=/run/user/$UBUNTU_UID --cwd /home/ubuntu/genesis -t -- bash -l"
echo ""
echo "    2. Run Claude Code (this is your first Genesis session):"
echo ""
echo "       claude"
echo ""
echo "       Claude Code will prompt you to log in (headless OAuth — it prints"
echo "       a URL to open in your browser). Once authenticated, Genesis will"
echo "       guide you through initial setup automatically."
echo ""
echo "       If onboarding doesn't start, run: /setup"
echo ""
echo "    3. Dashboard (available after setup completes):"
echo "       http://$CONTAINER_IP:5000/genesis"
echo ""
echo "  ─────────────────────────────────────────"
echo "  Note: Guardian (host-side health monitor) also needs Claude Code"
echo "  authentication for autonomous diagnosis. Run 'claude login' on the"
echo "  host when ready. This is optional — Guardian health checks work"
echo "  without it, but CC-powered diagnosis requires authentication."
echo ""
echo "  Reference"
echo ""
echo "  Container IP: $CONTAINER_IP"
echo "  Guardian:     systemctl --user status genesis-guardian.timer"
echo "  Manage:       incus start|stop|restart $CONTAINER_NAME"
echo ""
