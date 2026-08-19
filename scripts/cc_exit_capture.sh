#!/usr/bin/env bash
# cc_exit_capture.sh <slot> <exit_code>
#
# Records a Claude Code session's exit — the exit status (with a signal-decoded
# hint) plus a tail of the pane scrollback — to ~/.genesis/logs/cc_exit_<slot>.log.
#
# Why: a CC session runs as tmux `... exec claude` (see cc-slot.sh). When claude
# exits, the pane and its output vanish together, so a session that dies (a V8/
# Node fatal abort, a cgroup OOM kill, or just a clean exit) leaves NO durable
# trace — the 2026-08-19 session death was undiagnosable for exactly this reason
# (the kernel dmesg ring had cycled and the pane's dying words were already gone).
# cc-slot.sh drops the inner `exec` and calls this on claude's return so the exit
# is recorded before the pane closes.
#
# Best-effort by construction: it must NEVER fail the caller or change the
# session's exit code — the session is already on its way out.

set -u

slot="${1:-unknown}"
ec="${2:-unknown}"

# Resolve HOME under stripped env (same guard as cc-slot.sh); give up quietly if
# unresolvable — there is nowhere to log and this must not disrupt the exit.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
fi
[ -n "${HOME:-}" ] || exit 0

log_dir="${HOME}/.genesis/logs"
log="${log_dir}/cc_exit_${slot}.log"
mkdir -p "$log_dir" 2>/dev/null || exit 0

{
    echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) cc-${slot} claude exited status=${ec} ==="
    # Decode the common fatal exits (128 + signo) into a first-look diagnosis.
    case "$ec" in
        0)   echo "  (clean exit)" ;;
        130) echo "  (SIGINT — Ctrl-C)" ;;
        134) echo "  (SIGABRT — likely a V8/Node fatal abort, e.g. JS-heap OOM)" ;;
        137) echo "  (SIGKILL — likely a cgroup/OS OOM kill or forced termination)" ;;
        139) echo "  (SIGSEGV — native crash)" ;;
        143) echo "  (SIGTERM — terminated)" ;;
    esac
    # Pane scrollback tail — the dying words, if any reached the normal screen.
    # Guarded on TMUX_PANE so the script is harmless when run outside tmux.
    if [ -n "${TMUX_PANE:-}" ] && command -v tmux >/dev/null 2>&1; then
        echo "  --- pane tail (last 200 lines) ---"
        tmux capture-pane -p -t "$TMUX_PANE" -S -200 2>/dev/null | sed 's/^/  | /'
    fi
    echo
} >> "$log" 2>/dev/null || exit 0

# Self-rotation: keep the per-slot log bounded (last ~2000 lines ≈ several exits)
# so it never becomes an unbounded disk leak on a smaller install.
lines=$(wc -l < "$log" 2>/dev/null || echo 0)
if [ "${lines:-0}" -gt 2000 ]; then
    tail -n 2000 "$log" > "${log}.tmp" 2>/dev/null && mv "${log}.tmp" "$log" 2>/dev/null || true
fi
exit 0
