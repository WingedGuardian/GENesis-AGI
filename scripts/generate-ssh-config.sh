#!/usr/bin/env bash
# generate-ssh-config.sh — Generate client SSH config for tmux slot access.
#
# Auto-detects this machine's Tailscale hostname and outputs an SSH config
# snippet to paste into ~/.ssh/config on client devices.
#
# Usage: ./scripts/generate-ssh-config.sh

set -euo pipefail

if ! command -v tailscale &>/dev/null; then
    echo "Error: Tailscale is not installed." >&2
    exit 1
fi

TS_JSON=$(tailscale status --self --json 2>/dev/null) || {
    echo "Error: 'tailscale status --self --json' failed. Is Tailscale running?" >&2
    exit 1
}

TS_HOSTNAME=$(echo "$TS_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin)['Self']; print(d['DNSName'].split('.')[0])" 2>/dev/null)
TS_DNSNAME=$(echo "$TS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null)

if [[ -z "$TS_HOSTNAME" || -z "$TS_DNSNAME" ]]; then
    echo "Error: Could not determine Tailscale hostname." >&2
    exit 1
fi

REMOTE_USER=$(whoami)
REMOTE_HOME=$(eval echo "~${REMOTE_USER}")
SLOT_SCRIPT="${REMOTE_HOME}/genesis/scripts/cc-slot.sh"

cat << SSHEOF
# ─── Genesis tmux slots: ${TS_HOSTNAME} ───────────────────────
# Paste into ~/.ssh/config on your client devices.
# Usage: ssh ${TS_HOSTNAME}-1, ssh ${TS_HOSTNAME}-2, etc.
# Each slot maps to a persistent tmux session with claude.

Host ${TS_HOSTNAME}-*
    HostName ${TS_DNSNAME}
    User ${REMOTE_USER}
    RemoteCommand ${SLOT_SCRIPT} %n
    RequestTTY yes
    ServerAliveInterval 30
    ServerAliveCountMax 6

# Direct access (no slot, normal shell):
# Host ${TS_HOSTNAME}
#     HostName ${TS_DNSNAME}
#     User ${REMOTE_USER}
# ────────────────────────────────────────────────────────────────
SSHEOF

echo "" >&2
echo "Copy the above into ~/.ssh/config on your client devices." >&2
echo "Then: ssh ${TS_HOSTNAME}-1   (or any positive integer)" >&2
