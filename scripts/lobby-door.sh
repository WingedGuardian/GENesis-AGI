#!/usr/bin/env bash
# The LOBBY door: a FRESH picker over the live fleet, every connection.
#
# Invoked from the generated ssh_config as the RemoteCommand for
# `<host>-lobby` (see scripts/generate-ssh-config.sh). It is a script rather
# than an inline `tmux ... \; ...` chain for the same reason the numeric slot
# door is one: the inline form has to survive ssh_config -> remote shell ->
# tmux quoting, which is the documented failure mode of that block, and a
# script can be tested.
#
# WHY THIS DOOR DESTROYS NOTHING, which is the whole design
# --------------------------------------------------------
# Two facts about tmux, both MEASURED on a live install:
#
#   1. A pane MODE belongs to the PANE, not the client. `choose-tree` outlives
#      the client that opened it: the shared lobby pane was found at
#      `in_mode=1 mode=tree-mode attached=0`, and reconnecting landed INSIDE
#      the previous chooser. Re-issuing choose-tree does not clear it, and
#      neither does `send-keys -X cancel` — not even with a client attached,
#      where tmux answers "not in a mode" while `pane_in_mode` still reads 1.
#      (That is a statement about clearing it PROGRAMMATICALLY, which is what a
#      door can do. Whether a human pressing `q` at the keyboard exits the
#      chooser was not measured and is not what this door depends on.)
#
#   2. tmux sessions are SHARED. A second client attaching to a fixed session
#      name gets the SAME pane, so a second Fleet window drags the first into
#      its chooser. That is not hypothetical: it killed a live codex.
#
# An earlier design kept ONE persistent session named `lobby` and tried to make
# those two facts safe — respawning the pane to clear (1), and a lock plus an
# owner marker to serialise (2). Every defect this door has ever had came from
# that single choice, because it made the door DESTRUCTIVE and then needed a
# predicate for when destroying is safe. There is no such predicate: an
# unattached pane may hold live work, and `Ctrl-b s` opens the chooser OVER a
# running process, so even `mode=tree-mode` does not mean disposable (MEASURED:
# `mode=[tree-mode] cmd=sleep`).
#
# So the session is not shared and not reused. Each connection gets its OWN
# picker, named by pid, destroyed when it is left:
#
#   - never stale, because it is new every time (fact 1 cannot apply)
#   - never stolen, because no two connections share a name (fact 2 cannot apply)
#   - nothing is ever reset, respawned or killed, so no work can be lost
#
# A persistent scratch session is then just another session: the operator keeps
# one if they want one, this door never touches it, and it appears in the picker
# alongside every cc-* slot.
#
# MEASURED end-to-end: the client SURVIVES its own picker self-destructing —
# picking `cc-2` moved the client to cc-2 and the `lobby-<pid>` session vanished,
# leaving zero sessions behind. That is the property the whole design rests on.
set -uo pipefail

# The persistent container command line. It is one of the things the operator
# picks from the tree — "the lobby" as they think of it — so it must always be
# there to pick, including after a reboot. The old door got that for free by
# ATTACHING to it, which is what made it destructible; this one CREATES it if
# absent and then never touches it again.
#
# Idempotent, MEASURED against a session holding live work under a stale
# chooser: three runs left pane_pid, pane_mode and pane_current_command
# byte-identical. (`new-session -d -A` would do the same job but prints "open
# terminal failed: not a terminal" every time, so an existence test is used.)
#
# ⚠ This comment also used to say SILENT, and that was the load-bearing
# falsehood. The probe printed nothing HERE — its stderr was redirected — while
# tmux announced the miss to every OTHER attached client. "Prints nothing" was
# checked from the wrong end, and the claim then read as settled for anyone who
# came looking. Silence on your own stderr is not silence on the server.
# WHY NOT `has-session`, WHICH IS THE OBVIOUS SPELLING
# ----------------------------------------------------
# Because on tmux, "no such session" is an ERROR, and a tmux error is a
# SERVER-SIDE MESSAGE shown on every attached client's status line in
# `message-style` — which is `bg=yellow` here, against a green `status-style`.
# The `2>/dev/null` silences this PROCESS's stderr; it does not stop the server
# telling the other clients.
#
# MEASURED on tmux 3.4 with a client attached:
#   has-session -t "=lobby-555555" 2>/dev/null   -> +1 "can't find session"
#   list-sessions -F … | grep -qxF "lobby-555555" -> +0
# Same verdict, no message. With NO client attached neither emits, so a test
# for this MUST attach one or it proves nothing.
#
# This matters because the picker-name probe below runs on the SUCCESS path:
# the name is free every time, so "can't find session" fired on every single
# fleet connection, painting the operator's status line yellow. `display-time`
# is 750ms, so a warm connect finished painting after it expired and a cold one
# did not — which is exactly the intermittency that made this so hard to pin.
#
# Evidence it was really this: the operator's own `show-messages` log showed
#   has-session -t =lobby-1161923 / message: can't find session: lobby-1161923
#   / new-session / choose-tree / switch-client -Z -t =cc-5:
# — the chooser opening under the message, and the keypress meant to dismiss
# the message being eaten by choose-tree as "select the highlighted entry".
_session_exists() {
    # -F (fixed string) because a session name is free text, -x so `lobby` can
    # never match `lobby-123`. `list-sessions` failing (no server yet, first
    # boot) yields no output, grep fails, and the caller reads "absent" — which
    # is the correct answer in that state.
    # `grep -xF … >/dev/null`, NOT `grep -qxF`. This file runs under
    # `set -uo pipefail` (line 52), and -q exits on the first match — which can
    # SIGPIPE tmux mid-write and make the PIPELINE fail while the session
    # plainly exists. A false "absent" here is worse than the bug this function
    # was written for: the door would then create on a taken name, tmux would
    # answer `duplicate session`, and the login would die outright (MEASURED:
    # exit 1, no client attached).
    #
    # Honest about the evidence: I could NOT reproduce that failure — 0/40
    # false negatives with 301 sessions and the target sorted first, because
    # ~3.6KB of names fits the ~64KB pipe buffer, so tmux completes its write
    # before grep exits. It would take thousands of sessions to reach. The
    # change is kept because it costs nothing and the failure mode if that
    # bound is ever wrong is a dead login, not a cosmetic glitch.
    tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -xF "$1" >/dev/null
}

WORKSPACE="lobby"
_session_exists "$WORKSPACE" \
    || tmux new-session -d -s "$WORKSPACE" 2>/dev/null

# The PICKER is per-connection, so two live windows can never meet: one pid
# cannot open two doors. Concurrency is not the only way a name can be taken,
# though — a STALE `lobby-<pid>` can outlive its door if the chain below was
# interrupted between the create and the `destroy-unattached` that reaps it, and
# pids are reused. Then `new-session -A` would ATTACH to that orphan and the
# next command would arm destroy-unattached on it, so switching away destroys
# whatever it held. Same work-destroying class this whole door exists to end.
#
# Two changes close it. Step off a taken name here, and — below — create WITHOUT
# `-A`, so a name that becomes taken in the gap makes the door FAIL rather than
# silently adopt somebody else's session. Failing is a lost login; adopting is
# lost work.
SESSION="lobby-$$"
_n=0
while _session_exists "$SESSION"; do
    _n=$((_n + 1))
    SESSION="lobby-$$-${_n}"
    # A bound, not a fallback: 20 taken names means something is wrong that
    # another increment will not fix, and the un-`-A`'d create below still
    # refuses rather than adopting.
    [ "$_n" -ge 20 ] && break
done
unset _n

# `destroy-unattached` is set ON THIS SESSION (-t), never globally: a global set
# would reap every cc-* slot the moment its terminal window closed, which is the
# exact opposite of why the slots exist. It is set AFTER the attach, deliberately
# — MEASURED: setting it on a still-detached session destroys that session
# immediately, before any client can arrive.
# No `-A`: see above. On a name collision this FAILS instead of attaching to
# whatever is already there.
#
# The tree is FILTERED to hide every transient `lobby-<pid>` — including this
# one. Unfiltered, a second connection's picker is listed in this one's chooser,
# and selecting that innocuous-looking entry switches the client into the other
# throwaway session: two windows on one pane, which is the shared-pane defect
# this door was written to remove, restored through its own picker. MEASURED:
# the filter keeps `cc-*` and the persistent `lobby`, and drops `lobby-12345`
# and `lobby-67890`.
exec tmux -u new-session -s "$SESSION" \; \
    set-option -t "=${SESSION}:" destroy-unattached on \; \
    choose-tree -Zs -f '#{!=:#{m:lobby-*,#{session_name}},1}'
