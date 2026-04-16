#!/usr/bin/env bash
# cc-slot.sh — Persistent tmux slot for Claude Code sessions.
#
# Invoked by SSH RemoteCommand. Parses a hostname like "genesis-3-4"
# to extract the slot number, then attaches to (or creates) tmux
# session "cc-4" running claude in ~/genesis.
#
# Usage: cc-slot.sh <hostname>   (e.g., cc-slot.sh genesis-3-4)

set -euo pipefail

GENESIS_ROOT="${HOME}/genesis"
SESSION_PREFIX="cc"

# --- Parse slot number from hostname ---
if [[ $# -lt 1 ]]; then
    echo "Usage: cc-slot.sh <hostname>  (e.g., genesis-3-4)" >&2
    exit 1
fi

HOSTNAME_ARG="$1"
SLOT="${HOSTNAME_ARG##*-}"

if ! [[ "$SLOT" =~ ^[1-9][0-9]*$ ]]; then
    echo "Error: Invalid slot '$SLOT' (parsed from '$HOSTNAME_ARG')." >&2
    echo "Slot must be a positive integer (1, 2, 3, ...)." >&2
    exit 1
fi

SESSION_NAME="${SESSION_PREFIX}-${SLOT}"

# Handle nested tmux
unset TMUX

# --- Dynamic session cap (RAM + CPU aware) ---
RESERVED_MB=4096        # OS + Genesis runtime + background work headroom
PER_SESSION_MB=900      # Measured: ~800MB (claude + 4 MCP servers) + buffer
CPU_CAP=$(nproc)        # Never exceed core count (thrashing)

avail_kb=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
avail_mb=$((avail_kb / 1024))
ram_cap=$(( (avail_mb - RESERVED_MB) / PER_SESSION_MB ))

# Floor: 1 (never lock out); Ceiling: nproc
max_sessions=$ram_cap
[[ $max_sessions -lt 1 ]] && max_sessions=1
[[ $max_sessions -gt $CPU_CAP ]] && max_sessions=$CPU_CAP

existing=$(tmux list-sessions -F '#{session_name}' 2>/dev/null \
           | grep -c "^${SESSION_PREFIX}-" || echo 0)

# Reattaching to existing session — always allow
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    :  # bypass cap check
elif [[ $existing -ge $max_sessions ]]; then
    echo "ERROR: Session cap reached (${existing}/${max_sessions} active)." >&2
    echo "RAM: ${avail_mb}MB available, ${RESERVED_MB}MB reserved, ${PER_SESSION_MB}MB/session" >&2
    echo "" >&2
    echo "Active sessions:" >&2
    tmux list-sessions -F '  #{session_name}  (last activity: #{t:session_activity})' \
        2>/dev/null | grep "^  ${SESSION_PREFIX}-" >&2
    echo "" >&2
    echo "Kill an idle session: tmux kill-session -t ${SESSION_PREFIX}-<N>" >&2
    exit 1
fi

echo "→ Slot ${SLOT} (session: ${SESSION_NAME}, cap: ${existing}/${max_sessions})" >&2

exec tmux new-session -A -s "$SESSION_NAME" \
    -e "GENESIS_SLOT=${SLOT}" \
    "cd ${GENESIS_ROOT} && claude --dangerously-skip-permissions; exit"
