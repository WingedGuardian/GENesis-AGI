#!/usr/bin/env bash
# cc-slot.sh — Persistent tmux slot for Claude Code sessions.
#
# THE one interactive launcher: every door (SSH slot hostnames, manual SSH,
# the dashboard web terminal via the bashrc claude() wrapper) converges here,
# on the same attach-or-create tmux sessions. Idempotent by construction — a
# door walked twice attaches the SAME claude instead of spawning a second one.
#
# Usage: cc-slot.sh <hostname>               SSH RemoteCommand; parses a
#                                            hostname like "genesis-3-4" to
#                                            slot 4 -> session "cc-4"
#        cc-slot.sh manual [claude-args...]  manual/dashboard door: prints the
#                                            slot map, takes the LOWEST free
#                                            slot, forwards extra args to
#                                            claude inside the session

set -euo pipefail

# SSH RemoteCommand doesn't source .bashrc (interactive guard) — set PATH explicitly
export PATH="$HOME/.n/bin:$HOME/.bun/bin:$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"

# SSH from Windows sends no locale, so tmux marks the client non-UTF-8 and
# renders every non-ASCII glyph as "_". Force a UTF-8 locale for the client.
export LANG="${LANG:-C.UTF-8}"

GENESIS_ROOT="${HOME}/genesis"
SESSION_PREFIX="cc"

# --- Parse slot number from hostname (or allocate one in manual mode) ---
if [[ $# -lt 1 ]]; then
    echo "Usage: cc-slot.sh <hostname>            (e.g., genesis-3-4)" >&2
    echo "       cc-slot.sh manual [claude-args]  (lowest free slot)" >&2
    exit 1
fi

MODE_ARG="$1"
shift
# Extra claude args exist only in manual mode (SSH RemoteCommand passes %n only).
CLAUDE_EXTRA_ARGS=("$@")

if [[ "$MODE_ARG" == "manual" ]]; then
    # Manual/dashboard door. Show what already exists so reattach is the
    # visible easy path, then take the lowest slot with no live session.
    # New-by-default is deliberate: auto-reattach would trap "I want a fresh
    # session" in a loop; reattach stays one printed command away.
    slot_map=$(tmux list-sessions \
        -F '#{session_name}|#{session_attached}|#{t:session_activity}' \
        2>/dev/null | grep "^${SESSION_PREFIX}-" || true)
    if [[ -n "$slot_map" ]]; then
        echo "Existing slots (reattach: tmux attach -t <name>):" >&2
        while IFS='|' read -r name attached activity; do
            state="detached"
            [[ "$attached" -ge 1 ]] && state="attached"
            echo "  ${name}  ${state}  (last activity: ${activity})" >&2
        done <<<"$slot_map"
    fi
    SLOT=1
    # '=' forces exact-name match: a bare -t is prefix-matched by tmux, so
    # cc-1 would falsely read as existing whenever only cc-10 does.
    while tmux has-session -t "=${SESSION_PREFIX}-${SLOT}" 2>/dev/null; do
        SLOT=$((SLOT + 1))
    done
else
    SLOT="${MODE_ARG##*-}"

    if ! [[ "$SLOT" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: Invalid slot '$SLOT' (parsed from '$MODE_ARG')." >&2
        echo "Slot must be a positive integer (1, 2, 3, ...)." >&2
        exit 1
    fi
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
           | grep -c "^${SESSION_PREFIX}-" || true)

# Reattaching to existing session — always allow ('=' = exact-name match)
if tmux has-session -t "=$SESSION_NAME" 2>/dev/null; then
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

# Redirect CC temp to dedicated directory (keeps /tmp clean)
export TMPDIR="$HOME/.genesis/cc-tmp"
mkdir -p "$TMPDIR"
chmod 700 "$TMPDIR"

# Move CC's Bash sandbox off volatile /tmp onto persistent disk.
# CC uses CLAUDE_CODE_TMPDIR for its sandbox root (/claude-<uid>/<cwd>/).
# Without this, intermittent ENOENT failures on /tmp break the Bash tool.
export CLAUDE_CODE_TMPDIR="$HOME/.genesis/cc-tmp"

# Permission mode for this interactive dev console. Default: auto — auto-approves
# common ops but still prompts on deny/ask rules, which the operator answers in
# the tmux session (keeps deny-rule safety). To launch friction-free with
# --dangerously-skip-permissions, set GENESIS_CC_PERMISSION_MODE=bypass. SSH
# RemoteCommand does not source .bashrc, so this script also reads an optional
# ~/.genesis/cc-slot.env (e.g. a single line: GENESIS_CC_PERMISSION_MODE=bypass).
# Headless/autonomous CC sessions (CCInvoker -p) keep bypass separately — no
# human is present to answer a prompt.
if [ -f "${HOME}/.genesis/cc-slot.env" ]; then
    # Don't let a malformed override file kill the session under `set -e` (the
    # sourced file is the final command of an && chain, so a non-zero exit would
    # abort the script and drop the SSH session with no diagnostic).
    . "${HOME}/.genesis/cc-slot.env" \
        || echo "cc-slot: warning: ~/.genesis/cc-slot.env sourced with errors (continuing)" >&2
fi
case "${GENESIS_CC_PERMISSION_MODE:-auto}" in
    bypass|dangerous|skip) CC_PERM_FLAG="--dangerously-skip-permissions" ;;
    *)                     CC_PERM_FLAG="--permission-mode auto" ;;
esac

# Forwarded manual-mode args: shell-quote each one (%q) — the command below is
# a single string tmux hands to the default shell (bash on Genesis installs).
# A caller-supplied permission flag suppresses CC_PERM_FLAG so claude never
# receives two conflicting permission arguments.
CLAUDE_ARGS_Q=""
if [[ ${#CLAUDE_EXTRA_ARGS[@]} -gt 0 ]]; then
    for arg in "${CLAUDE_EXTRA_ARGS[@]}"; do
        case "$arg" in
            --dangerously-skip-permissions|--permission-mode|--permission-mode=*)
                CC_PERM_FLAG="" ;;
        esac
    done
    CLAUDE_ARGS_Q=$(printf ' %q' "${CLAUDE_EXTRA_ARGS[@]}")
fi

# -u: force UTF-8 output even if a future client's locale detection fails
exec tmux -u new-session -A -s "$SESSION_NAME" \
    -e "GENESIS_SLOT=${SLOT}" \
    -e "GENESIS_CC_PERMISSION_MODE=${GENESIS_CC_PERMISSION_MODE:-auto}" \
    -e "CLAUDE_CODE_TMPDIR=$CLAUDE_CODE_TMPDIR" \
    -e "LANG=$LANG" \
    "cd ${GENESIS_ROOT} && exec claude ${CC_PERM_FLAG}${CLAUDE_ARGS_Q}"
