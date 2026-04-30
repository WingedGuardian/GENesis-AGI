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
#   --ram SIZE             Memory limit (default: auto-scaled to host, max 24GiB)
#   --disk SIZE            Disk size (default: auto-scaled to host, max 30GB)
#   --cpus N               CPU limit (default: all host CPUs)
#   --repo URL             Genesis git repo URL (default: current remote)
#   --branch NAME          Branch to clone (default: main)
#   --non-interactive      Skip all prompts
#   -h, --help             Show this help
#
# Environment variables:
#   TAILSCALE_AUTH_KEY  Pre-auth key for headless/CI installs (skips interactive prompt)

set -euo pipefail

# Clear re-exec guard from parent environment (prevents sudo -E leak)
unset _GENESIS_REEXEC 2>/dev/null || true

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
INCUS_POOL_DIR=""  # auto-detected: set to /home/incus-data on split-disk VMs
_ORIG_ARGS="$*"
_install_ok=1      # tracks install.sh success
_guardian_ok=1     # tracks Guardian install success
_RAM_EXPLICIT=0    # set to 1 if user passed --ram
_DISK_EXPLICIT=0   # set to 1 if user passed --disk
_CPUS_EXPLICIT=0   # set to 1 if user passed --cpus

# ── Parse args ───────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --container-name) [ $# -ge 2 ] || { echo "ERROR: $1 requires a value"; exit 1; }; CONTAINER_NAME="$2"; shift ;;
        --ram)            [ $# -ge 2 ] || { echo "ERROR: $1 requires a value"; exit 1; }; RAM="$2"; _RAM_EXPLICIT=1; shift ;;
        --disk)           [ $# -ge 2 ] || { echo "ERROR: $1 requires a value"; exit 1; }; DISK="$2"; _DISK_EXPLICIT=1; shift ;;
        --cpus)           [ $# -ge 2 ] || { echo "ERROR: $1 requires a value"; exit 1; }; CPUS="$2"; _CPUS_EXPLICIT=1; shift ;;
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
_banner_ram="$RAM"; [ "$_RAM_EXPLICIT" = "0" ] && _banner_ram="auto (max 24GiB)"
_banner_disk="$DISK"; [ "$_DISK_EXPLICIT" = "0" ] && _banner_disk="auto (max 30GB)"
_banner_cpus="$CPUS"; [ "$_CPUS_EXPLICIT" = "0" ] && _banner_cpus="auto"
echo "  RAM: $_banner_ram | Disk: $_banner_disk | CPUs: $_banner_cpus"
echo "  Repo: $REPO_URL ($BRANCH)"
echo ""
echo "  TIP: This script is safe to re-run. If anything fails or you get"
echo "  disconnected, just run it again. When asked \"Delete and recreate?\","
echo "  answer \"no\" to keep existing data — only \"yes\" for a clean start."
echo ""

# ── Pre-flight (host level) ─────────────────────────────────
echo "  Pre-flight checks..."
PREFLIGHT_OK=1

# Root or sudo — prompt for password once if needed (caches for script duration)
if [ "$(id -u)" = "0" ]; then
    echo "    OK    Root access"
elif sudo -n true 2>/dev/null; then
    echo "    OK    Passwordless sudo"
elif [ "$NON_INTERACTIVE" = "0" ]; then
    echo "    Sudo requires a password. Enter it once — the script handles the rest."
    if sudo -v 2>/dev/null; then
        echo "    OK    Sudo authenticated"
    else
        echo "    FAIL  Sudo authentication failed"
        PREFLIGHT_OK=0
    fi
else
    echo "    FAIL  Need root or passwordless sudo (non-interactive mode)"
    PREFLIGHT_OK=0
fi

# OS check
if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo "    OK    OS: $PRETTY_NAME"
else
    echo "    WARN  Cannot determine OS (continuing anyway)"
fi

# Disk space on host — Incus stores containers on / by default.
# On GCP and other cloud VMs, /home may be on a larger separate disk.
# We detect this and route Incus storage accordingly.
root_avail_kb=$(df --output=avail / 2>/dev/null | tail -1 | tr -d ' ' || echo "0")
home_avail_kb=$(df --output=avail /home 2>/dev/null | tail -1 | tr -d ' ' || echo "0")
root_dev=$(df --output=source / 2>/dev/null | tail -1)
home_dev=$(df --output=source /home 2>/dev/null | tail -1)
if [ "$root_dev" != "$home_dev" ] && [ "${home_avail_kb:-0}" -gt "${root_avail_kb:-0}" ]; then
    # /home is on a separate, larger disk — Incus should store data there
    INCUS_POOL_DIR="/home/incus-data"
    check_avail_kb="$home_avail_kb"
    check_avail_h=$(df -h /home 2>/dev/null | tail -1 | awk '{print $4}')
    echo "    INFO  Split disk: root=$(df -h / | tail -1 | awk '{print $4}') free, /home=${check_avail_h} free"
    echo "          Incus storage will use /home/incus-data (larger disk)"
else
    INCUS_POOL_DIR=""
    check_avail_kb="$root_avail_kb"
    check_avail_h=$(df -h / 2>/dev/null | tail -1 | awk '{print $4}')
fi
if [ "${check_avail_kb:-0}" -lt 15728640 ] 2>/dev/null; then  # 15GB minimum
    echo "    FAIL  Need >= 15GB free for Incus storage, only ${check_avail_h} available"
    PREFLIGHT_OK=0
else
    echo "    OK    Disk: ${check_avail_h} free"
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

# ── Auto-scale container resources to host capacity ──────────
# Only adjusts defaults — explicit --ram/--disk/--cpus are never overridden.
# Reserve 3GB RAM and 5GB disk for host OS, Guardian, and headroom.
_HOST_RESERVE_RAM_GB=3
_HOST_RESERVE_DISK_GB=5
_autoscale_changed=0
_check_avail_gb=$((check_avail_kb / 1048576))

if [ "$_RAM_EXPLICIT" = "0" ]; then
    _auto_ram=$((host_mem_gb - _HOST_RESERVE_RAM_GB))
    # Cap at the default (no overallocation on large hosts)
    if [ "$_auto_ram" -gt 24 ]; then
        _auto_ram=24
    fi
    # Floor: 4GB minimum for Genesis + Qdrant + CC
    if [ "$_auto_ram" -lt 4 ]; then
        echo ""
        echo "  FATAL: Host has ${host_mem_gb}GB RAM. After reserving ${_HOST_RESERVE_RAM_GB}GB"
        echo "  for the host, only ${_auto_ram}GB remains — Genesis needs at least 4GB."
        echo "  Minimum host RAM: $((4 + _HOST_RESERVE_RAM_GB))GB."
        exit 1
    fi
    RAM="${_auto_ram}GiB"
    _autoscale_changed=1
fi

if [ "$_DISK_EXPLICIT" = "0" ]; then
    _auto_disk=$((_check_avail_gb - _HOST_RESERVE_DISK_GB))
    # Cap at the default
    if [ "$_auto_disk" -gt 30 ]; then
        _auto_disk=30
    fi
    # Floor: 10GB minimum
    if [ "$_auto_disk" -lt 10 ]; then
        echo ""
        echo "  FATAL: Only ${_check_avail_gb}GB disk available. After reserving"
        echo "  ${_HOST_RESERVE_DISK_GB}GB for the host, only ${_auto_disk}GB remains"
        echo "  — Genesis needs at least 10GB."
        exit 1
    fi
    DISK="${_auto_disk}GB"
    _autoscale_changed=1
fi

if [ "$_CPUS_EXPLICIT" = "0" ]; then
    _auto_cpus=$(nproc 2>/dev/null || echo 4)
    # No upper cap: unlike RAM/disk, CPU limits are soft cgroup caps — the
    # container can use all cores but won't starve the host. Background tasks
    # (awareness loop, reflection, triage) benefit from parallelism.
    # Floor at 2
    if [ "$_auto_cpus" -lt 2 ]; then
        _auto_cpus=2
    fi
    CPUS="$_auto_cpus"
    _autoscale_changed=1
fi

if [ "$_autoscale_changed" = "1" ]; then
    echo ""
    echo "  Auto-sized container resources to host capacity:"
    echo "    RAM:  $RAM  (${host_mem_gb}GB host - ${_HOST_RESERVE_RAM_GB}GB reserved)"
    echo "    Disk: $DISK  (${_check_avail_gb}GB available - ${_HOST_RESERVE_DISK_GB}GB reserved)"
    echo "    CPUs: $CPUS  ($(nproc 2>/dev/null || echo '?') available)"
    if [ "${_auto_ram:-99}" -lt 8 ]; then
        echo ""
        echo "    NOTE: Container RAM is ${RAM} — Genesis will work but may be"
        echo "    slow under concurrent load. 8GB+ recommended for production use."
    fi
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

    echo "  Initializing Incus..."
    if [ -n "${INCUS_POOL_DIR:-}" ]; then
        # Use a custom pool dir on the larger disk to avoid filling the root partition.
        sudo mkdir -p "$INCUS_POOL_DIR"
        sudo incus admin init --preseed << PRESEED
config: {}
networks:
- config:
    ipv4.address: auto
    ipv4.nat: "true"
    ipv6.address: none
  name: incusbr0
  type: bridge
storage_pools:
- config:
    source: $INCUS_POOL_DIR
  driver: dir
  name: default
profiles:
- config: {}
  devices:
    eth0:
      name: eth0
      network: incusbr0
      type: nic
    root:
      path: /
      pool: default
      type: disk
  name: default
PRESEED
        echo "  + Incus initialized (pool: $INCUS_POOL_DIR)"
    else
        sudo incus admin init --minimal
        echo "  + Incus initialized"
    fi

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
    if [ "${_GENESIS_REEXEC:-}" = "1" ]; then
        echo "  FATAL: incus-admin group activation failed after re-exec."
        echo "  Log out and back in, then re-run this script."
        exit 1
    fi
    echo "  Activating incus-admin group (re-running script)..."
    export _GENESIS_REEXEC=1
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

    # Quick health check on existing container to detect damage
    _container_healthy=1
    _container_issues=""
    _container_state=$(incus info "$CONTAINER_NAME" 2>/dev/null | grep -oP 'Status: \K\w+' || echo "unknown")

    if [ "$_container_state" = "RUNNING" ]; then
        # Check critical paths (timeout prevents hang on unresponsive containers)
        timeout 10 incus exec "$CONTAINER_NAME" -- test -w /tmp 2>/dev/null || {
            _container_healthy=0
            _container_issues="${_container_issues}    - /tmp is missing or not writable\n"
        }
        timeout 10 incus exec "$CONTAINER_NAME" -- test -d /home/ubuntu/genesis 2>/dev/null || {
            _container_healthy=0
            _container_issues="${_container_issues}    - Genesis repo not found\n"
        }
        timeout 10 incus exec "$CONTAINER_NAME" -- test -x /home/ubuntu/genesis/.venv/bin/python 2>/dev/null || {
            _container_healthy=0
            _container_issues="${_container_issues}    - Python venv missing or broken\n"
        }
    elif [ "$_container_state" = "STOPPED" ]; then
        echo "  Container is stopped — will start it."
        incus start "$CONTAINER_NAME" 2>/dev/null || true
        sleep 3
    else
        _container_healthy=0
        _container_issues="${_container_issues}    - Container in unexpected state: $_container_state\n"
    fi

    if [ "$_container_healthy" = "0" ]; then
        echo ""
        echo "  ┌──────────────────────────────────────────────────────────────┐"
        echo "  │  WARNING: Container appears damaged.                         │"
        echo "  └──────────────────────────────────────────────────────────────┘"
        echo ""
        echo -e "$_container_issues"
        echo "  Recreating will give you a clean install."
        echo "  Continuing will attempt to repair in-place (may not work)."
        echo ""
    fi

    if [ "$NON_INTERACTIVE" = "1" ]; then
        if [ "$_container_healthy" = "0" ]; then
            echo "  Container is damaged — recreating (non-interactive mode)."
            incus delete "$CONTAINER_NAME" --force
            echo "  + Old container deleted"
        else
            echo "  Continuing with existing container."
        fi
    else
        if [ "$_container_healthy" = "0" ]; then
            read -rp "  Delete and recreate? [Y/n] " _recreate || true
            if [ "${_recreate:-Y}" != "n" ] && [ "${_recreate:-Y}" != "N" ]; then
                incus delete "$CONTAINER_NAME" --force
                echo "  + Old container deleted"
            else
                echo "  Continuing with existing container (repair attempt)."
            fi
        else
            read -rp "  Delete and recreate? [y/N] " _recreate || true
            if [ "${_recreate:-N}" = "y" ] || [ "${_recreate:-N}" = "Y" ]; then
                incus delete "$CONTAINER_NAME" --force
                echo "  + Old container deleted"
            else
                echo "  Continuing with existing container."
            fi
        fi
    fi
fi

if ! incus info "$CONTAINER_NAME" &>/dev/null; then
    # Reset Guardian state — stale state from a previous container causes
    # "confirmed_dead" to persist even after a fresh container is created.
    _guardian_state="$HOME/.local/state/genesis-guardian/state.json"
    if [ -f "$_guardian_state" ]; then
        echo '{}' > "$_guardian_state"
        echo "  + Guardian state reset (stale from previous container)"
    fi

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

# ── Split-disk: ensure container home has adequate space ─────────────────────
# If /home is on a separate larger disk but the Incus pool was initialized on
# root (common when Incus was pre-installed before host-setup.sh ran, e.g. on
# GCP VMs with a 10GB root + 40GB /home), the container only sees the root
# disk's free space (~1-2GB) — not enough for Genesis's venv and dependencies.
# Fix: bind-mount a directory from the larger disk into the container's home.
if [ "$root_dev" != "$home_dev" ] && [ "${home_avail_kb:-0}" -gt "${root_avail_kb:-0}" ]; then
    _container_home_kb=$(incus exec "$CONTAINER_NAME" -- df --output=avail /home/ubuntu 2>/dev/null | tail -1 | tr -d ' ' || echo "0")
    if [ "${_container_home_kb:-0}" -lt 10485760 ]; then  # < 10GB free in container home
        echo ""
        echo "  Split disk: /home disk has more space than root ($(df -h /home | tail -1 | awk '{print $4}') vs $(df -h / | tail -1 | awk '{print $4}') free)."
        echo "  Container home has only $(( ${_container_home_kb:-0} / 1048576 ))GB free — binding larger disk in..."
        _home_bind_src="/home/genesis-home"
        sudo mkdir -p "$_home_bind_src"
        # Unprivileged Incus containers shift UIDs: container UID N → host UID (1000000 + N).
        # The container's ubuntu user (UID 1000) maps to host UID 1001000.
        _ubuntu_uid=$(incus exec "$CONTAINER_NAME" -- id -u ubuntu 2>/dev/null || echo "1000")
        _host_mapped_uid=$((1000000 + _ubuntu_uid))
        sudo chown "$_host_mapped_uid:$_host_mapped_uid" "$_home_bind_src"
        incus config device remove "$CONTAINER_NAME" homedisk 2>/dev/null || true
        incus config device add "$CONTAINER_NAME" homedisk disk source="$_home_bind_src" path=/home/ubuntu
        incus restart "$CONTAINER_NAME"
        sleep 3
        _post_kb=$(incus exec "$CONTAINER_NAME" -- df --output=avail /home/ubuntu 2>/dev/null | tail -1 | tr -d ' ' || echo "0")
        echo "  + Home disk bound ($(( ${_post_kb:-0} / 1048576 ))GB now free in container home)"
    fi
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
    for _i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
        sleep 2
        _CONTAINER_IP=$(incus list "$CONTAINER_NAME" -f csv -c 4 2>/dev/null | grep -oP '\d+\.\d+\.\d+\.\d+' | head -1 || true)
        [ -n "$_CONTAINER_IP" ] && break
    done
fi

if [ -z "$_CONTAINER_IP" ]; then
    echo "  FATAL: Container has no IPv4 address after 30s."
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

# ── Timezone detection ────────────────────────────────────────
# Detect timezone via IP geolocation, confirm with user, set on host + container.
# This is the one moment we have interactive access + sudo on both machines.
_detected_tz=""
_final_tz="UTC"

# Try IP geolocation (the install requires internet, so this should work)
_detected_tz=$(curl -sf --max-time 5 "http://ip-api.com/json?fields=timezone" 2>/dev/null | \
    grep -o '"timezone":"[^"]*"' | cut -d'"' -f4)

# Fallback: host system timezone (may be UTC on fresh cloud VMs)
if [ -z "$_detected_tz" ]; then
    _detected_tz=$(timedatectl show -p Timezone --value 2>/dev/null || echo "UTC")
fi

if [ "$NON_INTERACTIVE" = "0" ] && [ -n "$_detected_tz" ] && (exec </dev/tty) 2>/dev/null; then
    echo ""
    echo "  Detected timezone: $_detected_tz"
    read -r -p "  Is this correct? [Y/n] " _tz_confirm </dev/tty
    if [ "$_tz_confirm" = "n" ] || [ "$_tz_confirm" = "N" ]; then
        read -r -p "  Enter timezone (e.g., America/New_York, Europe/London): " _manual_tz </dev/tty
        # Validate against system timezone list
        if timedatectl list-timezones 2>/dev/null | grep -qx "$_manual_tz"; then
            _final_tz="$_manual_tz"
        else
            echo "  WARN: '$_manual_tz' not recognized. Using $_detected_tz."
            _final_tz="$_detected_tz"
        fi
    else
        _final_tz="$_detected_tz"
    fi
else
    # Non-interactive, no TTY, or detection failed — keep detected value or UTC default
    _final_tz="${_detected_tz:-UTC}"
    if [ -n "$_detected_tz" ] && [ "$_detected_tz" != "UTC" ]; then
        echo ""
        echo "  Detected timezone: $_detected_tz (auto-accepted, no TTY)"
    fi
fi

# Set on host VM
if [ "$_final_tz" != "UTC" ] && [ "$_final_tz" != "Etc/UTC" ]; then
    sudo timedatectl set-timezone "$_final_tz" 2>/dev/null || true
fi

# Container timezone is set later (right before install.sh) because
# apt-get with DEBIAN_FRONTEND=noninteractive reconfigures tzdata→UTC.

echo "  + Timezone: $_final_tz (host)"


# ── Set up user inside container ─────────────────────────────
echo "  Setting up user inside container..."

# Create ubuntu user if it doesn't exist
incus exec "$CONTAINER_NAME" -- bash -c 'id ubuntu &>/dev/null || useradd -m -s /bin/bash ubuntu' 2>/dev/null

# Persist HOME in /etc/environment so all sessions (including Claude Code)
# have it set without requiring a login shell.
incus exec "$CONTAINER_NAME" -- bash -c '
    grep -q "^HOME=" /etc/environment 2>/dev/null || echo "HOME=/home/ubuntu" >> /etc/environment
' 2>/dev/null

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
        python3 python3-pip \
        nodejs npm || { echo "  FATAL: package install failed"; exit 1; }
    # Install venv — try version-specific first (noble = 3.12), then generic
    apt-get install -y -q python3.12-venv 2>/dev/null || \
        apt-get install -y -q python3-venv || \
        { echo "  FATAL: could not install python3-venv"; exit 1; }
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

# ── Codebase visualization port forwarding ────────────────────────
# Forward host:9749 → container:9749 for the codebase-memory-mcp 3D graph UI.
# Only active when a Claude Code session is running (MCP server serves the UI).
if incus config device get "$CONTAINER_NAME" codebase-viz-proxy listen &>/dev/null; then
    echo "  + Codebase viz proxy already configured"
else
    if incus config device add "$CONTAINER_NAME" codebase-viz-proxy proxy \
        listen=tcp:0.0.0.0:9749 connect=tcp:127.0.0.1:9749 2>/dev/null; then
        echo "  + Codebase viz proxy: host:9749 → container:9749"
    else
        echo "  WARN: Could not set up codebase viz proxy — graph UI only reachable via container IP"
    fi
fi

# ── Tailscale (remote dashboard access) ──────────────────────────────────────
# Most Genesis installs are headless VMs. Tailscale gives immediate remote access
# to the dashboard from any device without port-forwarding or firewall changes.
if ! command -v tailscale &>/dev/null; then
    echo ""
    echo "  Installing Tailscale (remote dashboard access)..."
    if curl -fsSL https://tailscale.com/install.sh 2>/dev/null | sh 2>/dev/null; then
        echo "  + Tailscale installed"
    else
        echo "  WARN: Tailscale install failed — dashboard will require SSH tunnel"
        echo "        Manual install: https://tailscale.com/download"
    fi
fi

if command -v tailscale &>/dev/null && ! tailscale ip -4 &>/dev/null; then
    # Installed but not authenticated
    if [ -n "${TAILSCALE_AUTH_KEY:-}" ]; then
        sudo tailscale up --authkey "$TAILSCALE_AUTH_KEY" 2>/dev/null
    elif [ "$NON_INTERACTIVE" = "0" ]; then
        echo ""
        echo "  ┌──────────────────────────────────────────────────────────────┐"
        echo "  │  TAILSCALE: Authenticate to enable remote dashboard access.  │"
        echo "  │  Visit the URL below in your browser.                        │"
        echo "  │  (Free account at https://tailscale.com if you need one)     │"
        echo "  └──────────────────────────────────────────────────────────────┘"
        echo ""
        # tailscale up prints the auth URL directly to the terminal
        sudo tailscale up &
        _ts_pid=$!
        # Poll for auth completion (up to 120s)
        _ts_i=0
        while [ "$_ts_i" -lt 40 ]; do
            sleep 3
            _ts_i=$((_ts_i + 1))
            tailscale ip -4 &>/dev/null && break
        done
        # Kill the blocking tailscale up — it never exits without auth
        kill -0 "$_ts_pid" 2>/dev/null && sudo kill "$_ts_pid" 2>/dev/null
        wait "$_ts_pid" 2>/dev/null || true
        if tailscale ip -4 &>/dev/null; then
            echo "  + Tailscale authenticated: $(tailscale ip -4)"
        else
            echo "  WARN: Tailscale auth timed out. Run 'sudo tailscale up' later."
        fi
    else
        echo "  Tailscale installed but not authenticated (non-interactive mode)."
        echo "  Run 'sudo tailscale up' after setup to enable remote dashboard access."
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
    # Use -t (PTY) when we have a real terminal, -T (no PTY) otherwise.
    # install.sh uses `[ -t 0 ]` (should_prompt) to decide interactive mode.
    # If we pass -t without a real terminal, incus allocates a zombie PTY and
    # install.sh's reads block forever waiting for input that can't arrive.
    _incus_tty="-T"
    if (exec </dev/tty) 2>/dev/null; then
        _incus_tty="-t"
    fi

    # Set container timezone now (after apt-get which resets tzdata to UTC)
    if [ -n "$_final_tz" ] && [ "$_final_tz" != "UTC" ]; then
        incus exec "$CONTAINER_NAME" -- timedatectl set-timezone "$_final_tz" 2>/dev/null || \
            incus exec "$CONTAINER_NAME" -- ln -sf "/usr/share/zoneinfo/$_final_tz" /etc/localtime 2>/dev/null || true
        echo "  + Container timezone: $_final_tz"
    fi

    # shellcheck disable=SC2086  # Intentional: empty string should vanish
    incus exec "$CONTAINER_NAME" --user "$UBUNTU_UID" \
        --env "HOME=/home/ubuntu" \
        --env "XDG_RUNTIME_DIR=/run/user/$UBUNTU_UID" \
        --env "GENESIS_TIMEZONE=$_final_tz" \
        $_incus_tty --cwd /home/ubuntu/genesis -- \
        bash scripts/install.sh $_install_flags || {
        echo ""
        _install_ok=0
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

# ── Container smoke test ──────────────────────────────────────
# Verify the container is functional before proceeding to Guardian.
# Catches corruption from partial installs, botched manual interventions,
# or container filesystem issues (e.g., missing /tmp).
echo ""
echo "  Running container smoke test..."
_smoke_ok=0
_smoke_fail=""

# Run all checks in a single incus exec to avoid websocket handshake issues
# that occur with multiple separate incus exec calls (especially in nested
# pty environments like script, screen, or tmux wrappers).
_smoke_output=$(timeout -k 10 45 incus exec -T "$CONTAINER_NAME" \
    --user "$UBUNTU_UID" --env "HOME=/home/ubuntu" -- bash -c '
    test -w /tmp && echo "PASS_TMP" || echo "FAIL_TMP"
    python3 -c "print(42)" &>/dev/null && echo "PASS_PYTHON" || echo "FAIL_PYTHON"
    /home/ubuntu/genesis/.venv/bin/python -c "import genesis" &>/dev/null && echo "PASS_VENV" || echo "FAIL_VENV"
    curl --max-time 5 -sf http://localhost:6333/collections &>/dev/null && echo "PASS_QDRANT" || echo "FAIL_QDRANT"
    # Retry loop: genesis-server may still be starting on slow machines
    _code="000"
    for _try in 1 2 3 4 5; do
        _code=$(curl -so /dev/null -w "%{http_code}" --max-time 5 http://localhost:5000/api/genesis/health 2>/dev/null || echo "000")
        [ "$_code" = "200" ] || [ "$_code" = "503" ] && break
        sleep 2
    done
    if [ "$_code" = "200" ] || [ "$_code" = "503" ]; then echo "PASS_SERVER"; else echo "FAIL_SERVER"; fi
' 2>/dev/null || echo "FAIL_EXEC")

# Parse results — distinguish total exec failure from individual check failures
if echo "$_smoke_output" | grep -q "FAIL_EXEC"; then
    _smoke_fail="    FAIL  Could not execute smoke test in container (incus exec failed/timed out)\n"
else
    for _check in TMP PYTHON VENV QDRANT SERVER; do
        if echo "$_smoke_output" | grep -q "PASS_$_check"; then
            _smoke_ok=$((_smoke_ok + 1))
        else
            case "$_check" in
                TMP)     _smoke_fail="${_smoke_fail}    FAIL  /tmp is missing or not writable\n" ;;
                PYTHON)  _smoke_fail="${_smoke_fail}    FAIL  python3 not working\n" ;;
                VENV)    _smoke_fail="${_smoke_fail}    FAIL  genesis venv broken (cannot import genesis)\n" ;;
                QDRANT)  _smoke_fail="${_smoke_fail}    FAIL  Qdrant not responding on port 6333\n" ;;
                SERVER)  _smoke_fail="${_smoke_fail}    FAIL  genesis-server not responding on port 5000\n" ;;
            esac
        fi
    done
fi

if [ -z "$_smoke_fail" ]; then
    echo "  + Smoke test passed ($_smoke_ok/5 checks)"
else
    echo ""
    echo "  ┌──────────────────────────────────────────────────────────────┐"
    echo "  │  Container smoke test: $_smoke_ok/5 checks passed.               │"
    echo "  │  The container may be damaged from a partial install.        │"
    echo "  │  Consider re-running with a fresh container:                 │"
    echo "  │    sudo ./scripts/host-setup.sh  (answer 'y' to recreate)   │"
    echo "  └──────────────────────────────────────────────────────────────┘"
    echo ""
    echo -e "$_smoke_fail"
    echo ""
    echo "  Continuing anyway — Guardian and remaining steps may still work."
fi

# ── Persist network identity into user-level CLAUDE.md ─────────
# Write detected IPs into the container's ~/.claude/CLAUDE.md (user-level CC
# instructions, not tracked in the repo). Idempotent via sentinel blocks.
echo ""
echo "  Writing network identity into ~/.claude/CLAUDE.md..."
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

# Write into container via incus exec, piping the block through stdin.
# Target: ~/.claude/CLAUDE.md (user-level, not tracked in repo).
# Seed the file on first run; on subsequent runs only replace the
# network-identity sentinel block. <<\SEED prevents expansion inside
# the single-quoted bash -c.
echo "$_NET_LINES" | incus exec "$CONTAINER_NAME" --user "$UBUNTU_UID" \
    --env "HOME=/home/ubuntu" -- bash -c '
    _claude="$HOME/.claude/CLAUDE.md"
    mkdir -p "$HOME/.claude"
    # Seed file on first run (placeholder sections filled by later scripts)
    if [ ! -f "$_claude" ]; then
        cat > "$_claude" <<\SEED
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
SEED
    fi
    # Replace network-identity sentinel block with fresh data from stdin
    sed -i "/<!-- begin:network-identity -->/,/<!-- end:network-identity -->/d" "$_claude"
    {
        echo "<!-- begin:network-identity -->"
        cat   # consume stdin — _NET_LINES includes heading + content
        echo "<!-- end:network-identity -->"
    } >> "$_claude"
' 2>/dev/null && echo "  + Network identity written to ~/.claude/CLAUDE.md" || \
    echo "  WARN: Could not write network identity to ~/.claude/CLAUDE.md"

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
            _guardian_ok=0
            echo ""
            echo "  ┌──────────────────────────────────────────────────────────────┐"
            echo "  │  Guardian installation FAILED.                               │"
            echo "  │  Guardian is a core Genesis subsystem (health monitoring,    │"
            echo "  │  diagnosis, and recovery). Fix the error above and re-run:   │"
            echo "  └──────────────────────────────────────────────────────────────┘"
            echo "    bash $_guardian_script --container-name $CONTAINER_NAME"
            echo ""
        }
    else
        bash "$_guardian_script" $_guardian_flags || {
            _guardian_ok=0
            echo ""
            echo "  ┌──────────────────────────────────────────────────────────────┐"
            echo "  │  Guardian installation FAILED.                               │"
            echo "  │  Guardian is a core Genesis subsystem (health monitoring,    │"
            echo "  │  diagnosis, and recovery). Fix the error above and re-run:   │"
            echo "  └──────────────────────────────────────────────────────────────┘"
            echo "    bash $_guardian_script --container-name $CONTAINER_NAME"
            echo ""
        }
    fi
else
    echo "  WARNING: Guardian install script not found at $_guardian_script"
fi

# ── Node.js + Claude Code on host ─────────────────────────────
# Claude Code is installed inside the container by install.sh, but users also
# need it on the host VM for Guardian CC sessions and direct interaction.
# Node.js >= 20 is required by Claude Code.
echo ""
echo "  Setting up Claude Code on the host..."

_host_node_ok=0
if command -v node &>/dev/null; then
    _node_ver=$(node --version 2>/dev/null | grep -oP '(?<=v)\d+' | head -1)
    [ "${_node_ver:-0}" -ge 20 ] 2>/dev/null && _host_node_ok=1
fi

if [ "$_host_node_ok" = "0" ]; then
    echo "  Installing Node.js 20.x on host..."
    if command -v apt-get &>/dev/null; then
        if curl -fsSL https://deb.nodesource.com/setup_20.x 2>/dev/null | sudo -E bash - 2>/dev/null; then
            sudo apt-get install -y -qq nodejs 2>/dev/null
        fi
    elif command -v dnf &>/dev/null; then
        if curl -fsSL https://rpm.nodesource.com/setup_20.x 2>/dev/null | sudo bash - 2>/dev/null; then
            sudo dnf install -y nodejs 2>/dev/null
        fi
    fi
    if command -v node &>/dev/null; then
        echo "  + Node.js $(node --version) installed on host"
    else
        echo "  WARNING: Could not install Node.js on host."
        echo "  Claude Code won't be available on the host (still works inside the container)."
    fi
fi

CC_VERSION="${CC_VERSION:-2.1.87}"
if ! command -v claude &>/dev/null; then
    echo "  Installing Claude Code v${CC_VERSION} on host..."
    if command -v npm &>/dev/null; then
        sudo npm install -g "@anthropic-ai/claude-code@${CC_VERSION}" 2>/dev/null && \
            echo "  + Claude Code $(claude --version 2>/dev/null || echo "$CC_VERSION") installed on host" || \
            echo "  WARNING: npm install of Claude Code failed."
    else
        echo "  WARNING: npm not found — cannot install Claude Code on host."
    fi
else
    echo "  . Claude Code already on host ($(claude --version 2>/dev/null))"
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
if [ "$_install_ok" = "0" ]; then
    echo "  Genesis setup INCOMPLETE — install.sh had errors (see above)."
elif [ "$_guardian_ok" = "0" ]; then
    echo "  Genesis is ready (Guardian failed — see above)."
elif [ -n "${_smoke_fail:-}" ]; then
    echo "  Genesis setup completed with issues (see smoke test above)."
else
    echo "  Genesis is ready."
fi
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
echo "  GUARDIAN (host-side health monitor — always running):"
echo ""
echo "    Status: systemctl --user status genesis-guardian.timer"
echo "    Logs:   journalctl --user -u genesis-guardian -f"
if [ "$_guardian_cc_status" = "not authenticated" ]; then
    echo ""
    echo "    Agentic diagnosis: run 'claude login' on this host to enable"
    echo "    (Guardian monitors and alerts without it — diagnosis is an add-on)"
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
