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

# ── Error handling ──────────────────────────────────────────
# Print the failing line and command on any error so debugging
# doesn't require back-and-forth guesswork.
_on_error() {
    local exit_code=$?
    echo ""
    echo "  ──────────────────────────────────────────────────"
    echo "  FAILED at line $1"
    echo "  Command: $2"
    echo "  Exit code: $exit_code"
    echo "  ──────────────────────────────────────────────────"
    echo ""
    echo "  To re-run with full debug output:"
    echo "    DEBUG=1 ./scripts/host-setup.sh ${_ORIG_ARGS:-}"
    echo ""
}
trap '_on_error $LINENO "$BASH_COMMAND"' ERR

# DEBUG=1 enables bash tracing (set -x) for full command-by-command output
if [ "${DEBUG:-}" = "1" ]; then
    set -x
fi

# ── Defaults ─────────────────────────────────────────────────
CONTAINER_NAME="genesis"
RAM="24GiB"
DISK="30GB"
CPUS="8"
REPO_URL=""
BRANCH="main"
NON_INTERACTIVE=0
_ORIG_ARGS="$*"

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
echo "  TIP: This script is safe to re-run. If anything fails or you get"
echo "  disconnected, just run it again. When asked \"Delete and recreate?\","
echo "  answer \"no\" to keep existing data — only \"yes\" for a clean start."
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
    exec sg incus-admin -c "\"$(realpath "$0")\" $_ORIG_ARGS"
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

# ── Firewall: allow container traffic on bridge ─────────────
# Incus containers need three traffic paths allowed:
#   1. container ↔ host (DHCP, DNS to bridge dnsmasq) — INPUT/OUTPUT
#   2. container ↔ internet (forwarded traffic)       — FORWARD
# UFW 'deny (routed)' blocks #2, nftables may block #1.
# Rather than trying to detect which firewall is active and failing
# silently, just run ALL commands unconditionally. They're idempotent
# and fail harmlessly when the tool isn't installed.
_INCUS_BRIDGE=$(incus network list --format csv 2>/dev/null | grep ",YES," | head -1 | cut -d, -f1)
if [ -n "$_INCUS_BRIDGE" ]; then
    echo "  Configuring firewall for container traffic on $_INCUS_BRIDGE..."

    # UFW rules (no-op if ufw not installed or not active)
    sudo ufw allow in on "$_INCUS_BRIDGE" >/dev/null 2>&1 || true
    sudo ufw allow out on "$_INCUS_BRIDGE" >/dev/null 2>&1 || true
    sudo ufw route allow in on "$_INCUS_BRIDGE" >/dev/null 2>&1 || true
    sudo ufw route allow out on "$_INCUS_BRIDGE" >/dev/null 2>&1 || true

    # nftables rules — try all common table/chain combos
    for _table in "inet filter" "inet nftables_svc" "ip filter"; do
        for _chain in input forward; do
            sudo nft insert rule $_table $_chain iifname "$_INCUS_BRIDGE" accept 2>/dev/null || true
        done
    done

    # Verify: check if UFW rules actually took effect
    if command -v ufw &>/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
        if sudo ufw status 2>/dev/null | grep -q "on $_INCUS_BRIDGE"; then
            echo "  + Firewall: UFW rules applied for $_INCUS_BRIDGE"
        else
            echo "  WARN: UFW is active but rules didn't apply. Manual fix:"
            echo "    sudo ufw allow in on $_INCUS_BRIDGE"
            echo "    sudo ufw route allow in on $_INCUS_BRIDGE"
        fi
    else
        echo "  + Firewall: nftables rules applied for $_INCUS_BRIDGE"
    fi
else
    echo "  WARN: No managed Incus bridge found — firewall rules skipped"
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
# DHCP can be slow, especially on first boot or after UFW rules are freshly added.
# Actively kick the DHCP client if needed rather than just waiting and hoping.
echo "  Checking container networking..."
_CONTAINER_IP=$(incus list "$CONTAINER_NAME" -f csv -c 4 2>/dev/null | grep -oP '\d+\.\d+\.\d+\.\d+' | head -1 || true)
if [ -z "$_CONTAINER_IP" ]; then
    echo "  No IPv4 yet — reconfiguring network interface to trigger DHCP..."
    # networkctl reconfigure forces systemd-networkd to re-send DHCP Discover.
    # This handles the case where DHCP failed on boot (e.g. UFW was blocking it
    # and we just added the allow rules above).
    incus exec "$CONTAINER_NAME" -- networkctl reconfigure eth0 2>/dev/null || true
    for _i in 1 2 3 4 5 6; do
        sleep 2
        _CONTAINER_IP=$(incus list "$CONTAINER_NAME" -f csv -c 4 2>/dev/null | grep -oP '\d+\.\d+\.\d+\.\d+' | head -1 || true)
        [ -n "$_CONTAINER_IP" ] && break
    done
fi

if [ -z "$_CONTAINER_IP" ]; then
    echo "  FATAL: Container has no IPv4 address after 12s."
    echo ""
    echo "  This usually means the host firewall is blocking DHCP (UDP:67) on the"
    echo "  Incus bridge. UFW 'deny (incoming)' default drops DHCP Discover packets"
    echo "  from the container before dnsmasq can respond."
    echo ""
    echo "  Fix: sudo ufw allow in on ${_INCUS_BRIDGE:-incusbr0}"
    echo "       sudo ufw allow out on ${_INCUS_BRIDGE:-incusbr0}"
    echo "  Then re-run this script."
    exit 1
else
    echo "  + Container networking OK ($_CONTAINER_IP)"
fi

# Verify DNS works (IP alone isn't enough — apt/git need name resolution)
if ! incus exec "$CONTAINER_NAME" -- bash -c 'getent hosts github.com' &>/dev/null; then
    echo "  WARNING: Container has IPv4 but DNS is not resolving."
    echo "  Attempting to fix by restarting systemd-resolved..."
    incus exec "$CONTAINER_NAME" -- systemctl restart systemd-resolved 2>/dev/null || true
    sleep 2
    if ! incus exec "$CONTAINER_NAME" -- bash -c 'getent hosts github.com' &>/dev/null; then
        echo "  FATAL: DNS still not working. apt-get and git will fail."
        echo "  Debug: incus exec $CONTAINER_NAME -- resolvectl status"
        exit 1
    fi
fi
echo "  + DNS resolution OK"

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
    apt-get update -qq || { echo "  FATAL: apt-get update failed — check container DNS"; exit 1; }
    apt-get install -y -q \
        git curl sudo \
        python3 python3-pip python3.12-venv \
        nodejs npm || { echo "  FATAL: package install failed"; exit 1; }
' || {
    echo ""
    echo "  FATAL: Prerequisites installation failed inside container."
    echo "  Debug DNS:  incus exec $CONTAINER_NAME -- getent hosts archive.ubuntu.com"
    echo "  Debug ping: incus exec $CONTAINER_NAME -- ping -c1 8.8.8.8"
    echo "  Debug resolv: incus exec $CONTAINER_NAME -- cat /etc/resolv.conf"
    exit 1
}
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
    echo ""
    echo "  FATAL: Git clone failed."
    echo "  If this is a private repo, push code manually:"
    echo "    incus file push -r . ${CONTAINER_NAME}/home/ubuntu/genesis/"
    echo "  Then run install.sh inside the container."
    exit 1
}

# ── Dashboard port forwarding ──────────────────────────────────
# Forward host:5000 → container:5000 so the dashboard is reachable from
# the host's network interfaces (LAN, Tailscale, etc.), not just the
# internal container IP.
echo "  Setting up dashboard port forwarding..."
if incus config device get "$CONTAINER_NAME" dashboard-proxy listen &>/dev/null; then
    echo "  + Dashboard proxy already configured"
else
    if incus config device add "$CONTAINER_NAME" dashboard-proxy proxy \
        listen=tcp:0.0.0.0:5000 connect=tcp:127.0.0.1:5000 2>/dev/null; then
        echo "  + Dashboard proxy: host:5000 → container:5000"
    else
        echo "  WARN: Could not set up dashboard proxy — dashboard only reachable via container IP"
    fi
fi

# ── Detect network topology ───────────────────────────────────
# Figure out how the user will access the dashboard from their browser.
# Capture all available IPs for the final report and CLAUDE.md injection.
HOST_IPV4=""
HOST_IPV6=""
TS_IPV4=""
TS_IPV6=""

if command -v tailscale &>/dev/null; then
    TS_IPV4=$(tailscale ip -4 2>/dev/null || echo "")
    TS_IPV6=$(tailscale ip -6 2>/dev/null || echo "")
fi

LAN_IPV4=$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || echo "")
LAN_IPV6=$(ip -6 addr show scope global 2>/dev/null | grep -oP 'inet6 \K[^ /]+' | head -1 || echo "")

# Prefer Tailscale for primary host IP (reachable from anywhere on tailnet)
HOST_IPV4="${TS_IPV4:-$LAN_IPV4}"
HOST_IPV6="${TS_IPV6:-$LAN_IPV6}"

# Container IPs
CONTAINER_IPV4=$(incus exec "$CONTAINER_NAME" -- hostname -I 2>/dev/null | awk '{print $1}' || echo "")
CONTAINER_IPV6=$(incus exec "$CONTAINER_NAME" -- ip -6 addr show scope global 2>/dev/null \
    | grep -oP 'inet6 \K[^ /]+' | head -1 || echo "")

# Build dashboard URL for final report
DASHBOARD_URL=""
ACCESS_METHOD=""
if [ -n "$TS_IPV4" ]; then
    DASHBOARD_URL="http://$TS_IPV4:5000"
    ACCESS_METHOD="tailscale"
elif [ -n "$LAN_IPV4" ]; then
    DASHBOARD_URL="http://$LAN_IPV4:5000"
    ACCESS_METHOD="lan"
fi

if [ -n "$HOST_IPV4" ]; then
    echo "  + Host IP: $HOST_IPV4 (dashboard: http://$HOST_IPV4:5000)"
fi

# ── Run install.sh inside container ──────────────────────────
# Guard: only run if the repo actually exists
if incus exec "$CONTAINER_NAME" --user "$UBUNTU_UID" --env "HOME=/home/ubuntu" -- test -f /home/ubuntu/genesis/scripts/install.sh; then
    echo ""
    echo "  Running install.sh inside container..."
    echo "  ─────────────────────────────────────────"
    echo ""

    _install_flags=""
    if [ "$NON_INTERACTIVE" = "1" ]; then
        _install_flags="--non-interactive"
    fi
    # Note: we do NOT pass --force-interactive here. incus exec -t allocates a
    # pseudo-TTY, so install.sh's should_prompt ([ -t 0 ]) should work. If it
    # doesn't on a specific platform, the user can re-run install.sh inside the
    # container directly where TTY detection is reliable.

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

# ── Persist network identity into container CLAUDE.md ─────────
# Append detected IPs to the container's CLAUDE.md so CC sessions know the
# network topology. Idempotent: removes prior block before appending.
echo ""
echo "  Writing network identity into container CLAUDE.md..."
# Build the network block on the host side where all variables are available
_NET_LINES="## Network Identity"
_NET_LINES="${_NET_LINES}
"
_NET_LINES="${_NET_LINES}
- **Container IP**: ${CONTAINER_IPV4}"
[ -n "$CONTAINER_IPV6" ] && _NET_LINES="${_NET_LINES} (v6: ${CONTAINER_IPV6})"
_NET_LINES="${_NET_LINES}
- **Host VM IP**: ${HOST_IPV4}"
[ -n "$HOST_IPV6" ] && _NET_LINES="${_NET_LINES} (v6: ${HOST_IPV6})"
[ -n "$TS_IPV4" ] && _NET_LINES="${_NET_LINES}
- **Tailscale**: ${TS_IPV4}"
_NET_LINES="${_NET_LINES}
- **Dashboard**: http://${HOST_IPV4:-localhost}:5000 (via proxy device)"

# Write into container via incus exec, piping the block through stdin
echo "$_NET_LINES" | incus exec "$CONTAINER_NAME" --user "$UBUNTU_UID" \
    --env "HOME=/home/ubuntu" -- bash -c '
    _claude="/home/ubuntu/genesis/CLAUDE.md"
    # Remove prior network identity block if present
    if grep -q "^## Network Identity" "$_claude" 2>/dev/null; then
        sed -i "/^## Network Identity/,\$d" "$_claude"
    fi
    # Append new block from stdin
    echo "" >> "$_claude"
    cat >> "$_claude"
' 2>/dev/null && echo "  + Network identity written to container CLAUDE.md" || \
    echo "  WARN: Could not write network identity to container CLAUDE.md"

# ── Install Guardian on host ───────────────────────────────────
# Reuse CONTAINER_IPV4 detected earlier; fall back to incus list if empty
CONTAINER_IP="${CONTAINER_IPV4:-$(incus list "$CONTAINER_NAME" -f csv -c 4 2>/dev/null | grep -oP '\d+\.\d+\.\d+\.\d+' | head -1 || echo "unknown")}"

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

# ── Convenience alias on host ─────────────────────────────────
# The full incus exec command is long and easy to get wrong. Add a 'genesis'
# alias that sets HOME, XDG_RUNTIME_DIR, and cwd correctly.
_alias_user="${SUDO_USER:-$(whoami)}"
_alias_home=$(eval echo "~$_alias_user")
_alias_bashrc="$_alias_home/.bashrc"
if ! grep -q 'alias genesis=' "$_alias_bashrc" 2>/dev/null; then
    cat >> "$_alias_bashrc" << ALIASEOF

# Genesis container shell — full env for systemd + CC project discovery
alias genesis='incus exec $CONTAINER_NAME --user $UBUNTU_UID --env HOME=/home/ubuntu --env XDG_RUNTIME_DIR=/run/user/$UBUNTU_UID --cwd /home/ubuntu/genesis -t -- bash -l'
ALIASEOF
    echo "  + Added 'genesis' alias to $_alias_bashrc"
fi

# ── Report ───────────────────────────────────────────────────

# Check if Guardian CC was authenticated during install
_guardian_cc_status="not authenticated"
if command -v claude &>/dev/null && claude auth status &>/dev/null 2>&1; then
    _guardian_cc_status="authenticated"
fi

echo ""
echo "  ─────────────────────────────────────────"
echo "  Genesis is ready."
echo "  ─────────────────────────────────────────"
echo ""
echo "  STEP 1 — Connect to Genesis:"
echo ""
echo "    genesis"
echo ""
echo "    (This is an alias for the full incus exec command with all"
echo "    required environment variables. If 'genesis' isn't recognized,"
echo "    open a new terminal or run: source ~/.bashrc)"
echo ""
echo "  STEP 2 — Start your first session:"
echo ""
echo "    Inside the container, run:  claude"
echo "    Genesis will guide you through first-time setup"
echo "    (API keys, user profile, channels — all interactive)."
echo "    If onboarding doesn't start, run:  /setup"
echo ""
echo "  STEP 3 — Dashboard:"
echo ""
if [ "$ACCESS_METHOD" = "tailscale" ]; then
    echo "    $DASHBOARD_URL/genesis  (via Tailscale)"
elif [ -n "$DASHBOARD_URL" ]; then
    echo "    From this host:    http://localhost:5000/genesis"
    echo "    From your network: $DASHBOARD_URL/genesis"
    echo ""
    echo "    Can't reach it from your browser? Two options:"
    echo ""
    echo "    a) SSH tunnel (quick, no install):"
    echo "       ssh -L 5000:localhost:5000 <your-user>@$HOST_IPV4"
    echo "       Then open: http://localhost:5000/genesis"
    echo ""
    echo "    b) Tailscale (recommended for ongoing access):"
    echo "       curl -fsSL https://tailscale.com/install.sh | sh"
    echo "       sudo tailscale up"
    echo "       Then open: http://<tailscale-ip>:5000/genesis"
else
    echo "    http://$CONTAINER_IP:5000/genesis  (container IP — host only)"
fi
echo ""
echo "  GUARDIAN:"
echo ""
echo "    Status: systemctl --user status genesis-guardian.timer"
if [ "$_guardian_cc_status" = "not authenticated" ]; then
    echo "    AI diagnosis: not yet enabled"
    echo "    To enable:    claude login  (run on this host, outside the container)"
else
    echo "    AI diagnosis: enabled (Claude Code authenticated)"
fi
echo ""
echo "  NETWORK:"
echo ""
echo "    Container: $CONTAINER_NAME ($CONTAINER_IPV4)"
[ -n "$CONTAINER_IPV6" ] && echo "                IPv6: $CONTAINER_IPV6"
echo "    Host:      $HOST_IPV4"
[ -n "$HOST_IPV6" ] && echo "                IPv6: $HOST_IPV6"
[ -n "$TS_IPV4" ] && echo "    Tailscale: $TS_IPV4"
echo ""
echo "  MANAGE:"
echo ""
echo "    incus start|stop|restart $CONTAINER_NAME"
echo ""
