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

# The captured pane tail can contain prompts, command output, or credentials, so
# keep the log owner-only. umask covers newly-created dir/file; the explicit
# chmod below also tightens a log that predates this hardening.
umask 077

slot="${1:-unknown}"
ec="${2:-unknown}"

# Resolve HOME under stripped env (same guard as cc-slot.sh); give up quietly if
# unresolvable — there is nowhere to log and this must not disrupt the exit.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
fi
[ -n "${HOME:-}" ] || exit 0

# Scrub filter for the pane tail. secret_scrub is stdlib-only by design, so ANY
# python3 can run it — deliberately NOT the venv interpreter, which may be
# absent or broken on the path a dying session takes. Resolved relative to this
# script so a worktree checkout scrubs with its own copy.
#
# Returns non-zero (emitting nothing) when it cannot scrub, which the caller
# turns into a withheld-tail marker. It must NEVER pass input through on error.
_CC_HOOKS_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/hooks"

_cc_scrub_stdin() {
    [ -r "${_CC_HOOKS_DIR}/secret_scrub.py" ] || return 1
    command -v python3 >/dev/null 2>&1 || return 1
    # Read BYTES and decode leniently: the tail exists to capture a CRASHING
    # session, which is exactly the kind that emits a stray non-UTF-8 byte, and
    # a strict decode would discard the entire diagnostic over one of them.
    #
    # Wall-clock belt (fail-closed: timeout's 124 is non-zero, so the caller
    # withholds the tail exactly as for any other scrub failure). 10s is >100x
    # the measured full-matrix worst case; the failure mode it bounds is a
    # future pattern edit going super-linear on a shape the perf matrix does
    # not span — the one hang this path cannot otherwise exclude.
    _to=""
    command -v timeout >/dev/null 2>&1 && _to="timeout 10"
    $_to python3 -c 'import sys
sys.path.insert(0, sys.argv[1])
from secret_scrub import scrub
sys.stdout.write(scrub(sys.stdin.buffer.read().decode("utf-8", "replace")))' "$_CC_HOOKS_DIR"
}

log_dir="${HOME}/.genesis/logs"
log="${log_dir}/cc_exit_${slot}.log"
mkdir -p "$log_dir" 2>/dev/null || exit 0
# Enforce owner-only on the log even if it predates this change (umask only
# affects freshly-created files). Best-effort — never fail the caller.
[ -e "$log" ] && chmod 600 "$log" 2>/dev/null || true

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
        # The tail is RAW terminal output: whatever the session printed, a
        # freshly-minted credential included. Scrub it through the same
        # stdlib-only helper the capture hooks use (no venv needed — it imports
        # nothing outside the standard library, so a broken venv cannot break
        # this path). FAIL CLOSED: if the scrubber cannot run, WITHHOLD the tail
        # rather than write it raw. secret_scrub.scrub() already withholds its
        # own content on an internal error; this mirrors that choice one level
        # up, for the case where python itself is unavailable. The exit-status
        # lines above are always written either way, so the primary diagnostic
        # (why did the session die) survives even with no tail at all.
        # -J JOINS soft-wrapped rows back into the logical line the program
        # actually printed. Without it the terminal's wrap is a REDACTION HOLE:
        # a credential that starts near the right edge arrives as two separate
        # rows, neither of which carries a matchable prefix, so the token is
        # persisted in reconstructable form. MEASURED on tmux 3.4 at width 80:
        # a 300-char token is three unmatched rows without -J and one intact
        # line with it.
        if _tail=$(tmux capture-pane -p -J -t "$TMUX_PANE" -S -200 2>/dev/null) \
           && [ -n "$_tail" ]; then
            # Deliberately if/else, not `A && B || C`: in the chain form a
            # failure in the PRINT step (disk full, SIGPIPE) also fires C and
            # appends a "withheld" marker after a half-written tail.
            # 256KB cap on the filter input. -S -200 counts PHYSICAL rows either
            # way, so joining changes where the newlines fall, not the byte
            # total: a real tail stays height*width (~40KB) and the cap bites
            # only a pathological geometry.
            #
            # When the cap DOES bite, keep the NEWEST bytes: the newest rows
            # sit at the BOTTOM of the capture, and the dying words are the
            # whole point of this log — a cap taken from the top kept the
            # oldest scrollback and discarded them (while the marker claimed
            # the opposite). The leading PARTIAL line is dropped rather than
            # handed on: a byte cut lands anywhere, and several patterns need
            # their whole value on one line — a URL credential is recognised
            # by its `://u:pw@host` shape, so a line cut mid-URL could show a
            # pattern half a value. Cutting on a line boundary means the
            # over-long line is dropped whole, never half-shown.
            _cap="${GENESIS_CC_TAIL_CAP:-262144}"
            _feed=$(printf '%s\n' "$_tail")
            if [ "$(printf '%s' "$_feed" | wc -c)" -gt "$_cap" ]; then
                _feed=$(printf '%s' "$_feed" | tail -c "$_cap" | sed '1d')
                _truncated=1
            else
                _truncated=0
            fi
            if _scrubbed=$(printf '%s\n' "$_feed" | _cc_scrub_stdin 2>/dev/null); then
                printf '%s\n' "$_scrubbed" | sed 's/^/  | /'
                [ "$_truncated" = "1" ] && \
                    echo "  | [earlier output dropped: tail exceeded ${_cap} bytes]"
            else
                echo "  | [tail withheld: scrubber unavailable]"
            fi
        fi
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
