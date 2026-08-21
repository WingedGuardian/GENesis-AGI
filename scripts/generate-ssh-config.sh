#!/usr/bin/env bash
# generate-ssh-config.sh — Generate client SSH config for tmux slot access.
#
# Auto-detects this machine's Tailscale identity and outputs an SSH config
# snippet (keyed on the stable Tailscale IP, DNS-resolver-independent) to paste
# into ~/.ssh/config on client devices.
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
# HostName is keyed on the stable Tailscale IPv4, NOT the MagicDNS name: the IP
# is pinned per-device and routes without the client's DNS resolver, so the
# config keeps working even when MagicDNS/NRPT is disrupted on the client (a
# common Tailscale-on-Windows failure). The `${TS_HOSTNAME}-*` aliases are
# unchanged — only the resolved address is an IP. Re-run this script if the
# node's IP ever changes (device removed + re-added to the tailnet).
TS_IP=$(echo "$TS_JSON" | python3 -c "import sys,json; ips=json.load(sys.stdin)['Self'].get('TailscaleIPs') or []; print(next((i for i in ips if ':' not in i), ''))" 2>/dev/null)

if [[ -z "$TS_HOSTNAME" || -z "$TS_DNSNAME" || -z "$TS_IP" ]]; then
    echo "Error: Could not determine Tailscale identity (hostname/IPv4)." >&2
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
# Or:    ssh ${TS_HOSTNAME}-lobby  → opens the session picker; pick any live slot.

# One-click "lobby": a stable landing session that sees ALL live cc-* slots.
# After a client/reboot, ONE reconnect here brings the whole fleet back — the
# slots never died (they live in tmux on the box). It opens straight into the
# session picker (choose-tree): pick a slot and jump into it, in the state you
# left it; Ctrl-b s reopens the picker anytime.
#
# This specific block MUST precede the wildcard below: ssh uses the FIRST
# matching RemoteCommand, and the wildcard would otherwise route
# "${TS_HOSTNAME}-lobby" into cc-slot.sh, which rejects the non-numeric slot.
# 'lobby' is not a cc-N name, so it never consumes a slot or the slot cap.
# The RemoteCommand sets PATH (an ssh RemoteCommand does NOT source .bashrc), so
# tmux resolves even where it is user-local — mirrors cc-slot.sh's toolchain PATH
# so the lobby door matches the numeric-slot door's behavior.
Host ${TS_HOSTNAME}-lobby
    HostName ${TS_IP}
    User ${REMOTE_USER}
    RemoteCommand PATH="${REMOTE_HOME}/.n/bin:${REMOTE_HOME}/.bun/bin:${REMOTE_HOME}/.npm-global/bin:${REMOTE_HOME}/.local/bin:\$PATH" tmux -u new-session -A -s lobby \; choose-tree -Zs
    RequestTTY yes
    ServerAliveInterval 30
    ServerAliveCountMax 6

Host ${TS_HOSTNAME}-*
    HostName ${TS_IP}
    User ${REMOTE_USER}
    RemoteCommand ${SLOT_SCRIPT} %n
    RequestTTY yes
    ServerAliveInterval 30
    ServerAliveCountMax 6

# Direct access (no slot, normal shell):
# Host ${TS_HOSTNAME}
#     HostName ${TS_IP}
#     User ${REMOTE_USER}
# ────────────────────────────────────────────────────────────────
SSHEOF

echo "" >&2
echo "Copy the above into ~/.ssh/config on your client devices — REPLACING any" >&2
echo "earlier Genesis block for ${TS_HOSTNAME} (don't append a second one, or the" >&2
echo "old block would win by ssh's first-match rule)." >&2
echo "Then: ssh ${TS_HOSTNAME}-1        (a specific slot, or any positive integer)" >&2
echo "  or: ssh ${TS_HOSTNAME}-lobby    (opens the session picker → pick any live slot)" >&2
echo "" >&2
echo "Windows one-click: create a shortcut whose target is" >&2
echo "    wt.exe ssh ${TS_HOSTNAME}-lobby     (or:  ssh.exe ${TS_HOSTNAME}-lobby)" >&2
echo "then pin it to the taskbar — double-click reattaches the whole fleet." >&2
