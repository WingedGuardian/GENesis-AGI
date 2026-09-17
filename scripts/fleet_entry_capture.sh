#!/usr/bin/env bash
# fleet_entry_capture.sh <label>
#
# Records the fleet's PANE-MODE state at the moment a connection enters, to
# ~/.genesis/logs/fleet_entry_<YYYY-MM-DD>.log.
#
# WHY THIS EXISTS
# ---------------
# An operator intermittently lands in what looks like a frozen session: a row
# highlighted in yellow, keys doing nothing shell-like. The yellow is tmux's
# `mode-style` (default bg=yellow), NOT the status line — so the pane is in a
# MODE, almost certainly `choose-tree`. The session is not frozen; it is sitting
# in a chooser and the keystrokes are chooser keys.
#
# scripts/lobby-door.sh already documents the mechanism in its own header: a
# pane mode belongs to the PANE, not the client, so a mode outlives the
# disconnect. That door fixed it for the LOBBY pane by giving every connection
# its own throwaway picker. It did nothing for the panes the picker SELECTS, and
# those are reachable from both entries (`-lobby` and `-<N>`).
#
# MEASURED 2026-09-17, tmux 3.4, five arms against a scratch -L server with a
# fresh-pane control reading in_mode=0:
#
#   choose-tree on a cc-* pane, client attached  -> in_mode=1 mode=tree-mode
#   detach the client                            -> mode PERSISTS, attached=0
#   send-keys -X cancel   (client attached)      -> fails, "not in a mode"
#   send-keys -X cancel   (client detached)      -> fails, identically
#   send-keys q / send-keys Escape               -> both fail
#
# So a slot pane can hold a mode indefinitely and no PROGRAMMATIC clear works.
# Whether a human pressing `q` at the keyboard escapes is still UNMEASURED —
# writing to a client pty exercises the OUTPUT side and cannot inject input, so
# that arm is inconclusive rather than negative. This script deliberately does
# not depend on the answer.
#
# WHAT IT DOES AND DOES NOT DO
# ----------------------------
# It OBSERVES. It changes no pane, kills nothing, and alters no entry decision —
# the doors' destroys-nothing guarantee is untouched. The condition is
# intermittent and has never been captured live (the one time it was seen, it
# was cleared by hand before anything was recorded, and the investigation ended
# there). This makes the next occurrence a recorded fact instead of a memory.
#
# Best-effort by construction: it must NEVER fail its caller, change an exit
# code, or delay a login. Every failure path exits 0 silently.

set -u

# The log can name sessions and command basenames, so keep it owner-only. umask
# covers newly created dir/file; the chmod below also tightens a log written
# before this hardening.
umask 077

label="${1:-unknown}"

# Resolve HOME under stripped env (an ssh RemoteCommand does not source
# .bashrc). Give up quietly if unresolvable — there is nowhere to log, and a
# login must not be disrupted over a diagnostic.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
fi
[ -n "${HOME:-}" ] || exit 0

command -v tmux >/dev/null 2>&1 || exit 0

log_dir="${HOME}/.genesis/logs"
mkdir -p "$log_dir" 2>/dev/null || exit 0
log_file="${log_dir}/fleet_entry_$(date -u +%Y-%m-%d).log"

# ONE AGGREGATE DEADLINE for the whole capture, not a per-read belt.
#
# The failure mode bounded is a WEDGED tmux server turning a diagnostic into a
# hung interactive login — the one hang this path cannot otherwise exclude. 5s
# is >50x the measured local query latency (sub-100ms for list-panes on this
# install), so it cannot clip a healthy read.
#
# A per-read belt does NOT bound a login. MEASURED: with three serial reads each
# carrying `timeout 5`, a stubbed hanging tmux made this script take 15s, not 5
# — the operator pays the SUM. Each read now gets only what is LEFT of the
# budget, and once it is spent the remaining reads are skipped rather than
# started. Same defect class as a hook whose per-call timeouts cannot bound the
# aggregate against its registration.
#
# Pane data is read FIRST, so a partial budget still buys the field that
# matters; sessions and clients are context and are the right things to lose.
_CAPTURE_BUDGET_S=5
_capture_start="$(date +%s)"
_have_timeout=""
command -v timeout >/dev/null 2>&1 && _have_timeout=1

_tmux_read() {
    local remaining
    remaining=$(( _CAPTURE_BUDGET_S - ( $(date +%s) - _capture_start ) ))
    [ "$remaining" -gt 0 ] || return 0
    if [ -n "$_have_timeout" ]; then
        timeout "$remaining" tmux "$@" 2>/dev/null || true
    else
        # No `timeout` binary: the read is unbounded. Degrading to "skip the
        # capture entirely" would be worse — it would silently disable the
        # diagnostic on any install lacking coreutils' timeout, which is the
        # install least likely to notice.
        tmux "$@" 2>/dev/null || true
    fi
}

# INJECTABLE SEAM, for tests only. With GENESIS_FLEET_CAPTURE_PANES_FILE set to
# a readable file, pane lines are read from it instead of from tmux — so the
# anomaly CLASSIFICATION below can be exercised on a runner with no tmux at all,
# rather than hidden behind a skip guard that would make CI prove nothing. It
# changes only which lines are read for logging; it alters no entry decision,
# and there is no decision here to alter. Unset in every real invocation.
if [ -n "${GENESIS_FLEET_CAPTURE_PANES_FILE:-}" ] \
    && [ -r "${GENESIS_FLEET_CAPTURE_PANES_FILE}" ]; then
    panes="$(cat "${GENESIS_FLEET_CAPTURE_PANES_FILE}" 2>/dev/null)"
    sessions=""
    clients=""
else
    # The two DECISION fields lead, and both are generated by tmux rather than
    # by the operator: the in-mode flag, then tmux's OWN verdict on whether this
    # session is one of the doors' transient pickers.
    #
    # A session name is free text and may contain SPACES, so any parse that
    # searches the whole line can be forged: a session named
    # `x in_mode=1 mode=tree-mode y` would otherwise produce an ANOMALY while
    # sitting at in_mode=0. Leading with fixed-width machine fields makes
    # `${line%% *}` unambiguous whatever the name contains.
    #
    # The picker test uses `#{m:lobby-*,…}` — the SAME matcher lobby-door.sh
    # filters its own chooser with — so the two cannot drift. Re-implementing
    # that glob in shell is what invited the forgery in the first place.
    panes="$(_tmux_read list-panes -a -F \
        'in_mode=#{pane_in_mode} kind=#{?#{m:lobby-*,#{session_name}},picker,dest} #{session_name}:#{window_index}.#{pane_index} mode=#{pane_mode} attached=#{session_attached} pid=#{pane_pid} cmd=#{pane_current_command}')"
    sessions="$(_tmux_read list-sessions -F \
        '#{session_name} created=#{session_created} attached=#{session_attached}')"
    clients="$(_tmux_read list-clients -F '#{client_tty} session=#{client_session}')"
fi

# No server, or every read timed out: nothing to say. A cold first connection
# legitimately lands here, so this is silence rather than a record.
[ -n "$panes" ] || exit 0

# A pane in a mode is only an ANOMALY when it is a pane the operator can be
# dropped into. The doors' own transient pickers are named `lobby-<pid>` and are
# in tree-mode BY DESIGN — lobby-door.sh hides them from its own chooser with
# the same distinction (`-f '#{!=:#{m:lobby-*,#{session_name}},1}'`). The
# persistent workspace session is `lobby` with no suffix, which that glob does
# NOT match, so it stays selectable and stays in scope here.
anomalies=""
while IFS= read -r line; do
    [ -n "$line" ] || continue
    # Field 1: the in-mode flag. Field 2: tmux's picker/dest verdict. Both are
    # tmux-generated and space-free, so this parse cannot be influenced by a
    # session name.
    _flag="${line%% *}"
    _rest="${line#* }"
    _kind="${_rest%% *}"
    [ "$_flag" = "in_mode=1" ] || continue
    [ "$_kind" = "kind=dest" ] || continue
    # Record the line WITHOUT the two decision fields: "ANOMALY
    # moded-destination" already states both, and what a reader needs next is
    # the session, which should lead.
    anomalies="${anomalies}${_rest#* }
"
done <<EOF
$panes
EOF
unset _flag _rest _kind

{
    echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) entry=${label} pid=$$"
    if [ -n "$anomalies" ]; then
        # Greppable and unambiguous: this is the line to search for after the
        # operator next reports a yellow frozen session.
        printf '%s' "$anomalies" | while IFS= read -r a; do
            [ -n "$a" ] && echo "ANOMALY moded-destination ${a}"
        done
    fi
    echo "$panes" | sed 's/^/  pane /'
    [ -n "$sessions" ] && echo "$sessions" | sed 's/^/  sess /'
    [ -n "$clients" ] && echo "$clients" | sed 's/^/  client /'
} >>"$log_file" 2>/dev/null || exit 0

chmod 600 "$log_file" 2>/dev/null || true

exit 0
