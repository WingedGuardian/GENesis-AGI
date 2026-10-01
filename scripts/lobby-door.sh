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
# falsehood. "Prints nothing" had been checked from the wrong end: the probe's
# own stderr was redirected, so the caller saw silence either way. That much
# still stands. What followed from it did not — see the correction below.
# WHY NOT `has-session`, WHICH IS THE OBVIOUS SPELLING
# ----------------------------------------------------
# Because on tmux, "no such session" is an ERROR, and a tmux error is recorded
# in the SERVER's message log. MEASURED on tmux 3.4 with a client attached:
#   has-session -t "=lobby-555555" 2>/dev/null    -> +1 "can't find session"
#   list-sessions -F … | grep -qxF "lobby-555555" -> +0
# Same verdict, no log entry. The probe below runs on the SUCCESS path — the
# name is free every time — so one entry accumulated per fleet connection.
#
# CORRECTION, 2026-09-25. This block used to go much further, and it was wrong.
# It said the error is "shown on every attached client's status line in
# `message-style` (bg=yellow)", that `2>/dev/null` therefore could not stop it,
# and that `display-time` 750ms explained the intermittency. MEASURED and
# REFUTED: with a client attached through a pty and the capture read after the
# client exits, three absent probes added three log entries and ZERO bytes to
# that client's stream, while a `display-message` control on the same client
# painted at row 24 in black-on-yellow, 1/1. A door invoked as an ssh
# RemoteCommand runs tmux as a client with NO session, so the error goes to
# that client's own stderr — which the redirect did suppress.
#
# The yellow bar the operator actually saw was NOT this. It was `choose-tree`'s
# own `(search)` prompt, opened by a `?` byte from their terminal's DECRQM
# reply being delivered as a keystroke; the same reply's digit then chose an
# entry. That is the bug, and it is fixed by not landing in a chooser at all —
# see lobby-picker.sh, which this door now runs.
#
# What the `show-messages` log genuinely showed is the sequence, not a paint:
#   has-session -t =lobby-1161923 / message: can't find session: lobby-1161923
#   / new-session / choose-tree / switch-client -Z -t =cc-5:
# The switch at the end is the stolen selection. The message above it was
# coincident, not causal — which is exactly the tidy story a surprising
# observation invites, and the reason the first two fixes did not hold.
#
# Keeping the silent probe anyway: it removes per-connection log noise, and an
# erroring probe on a path where absent is the expected answer is wrong on its
# own terms. It is simply not what was painting anything.
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
# The landing screen is a LINE-based picker, not choose-tree, and that is a
# security-of-input decision rather than a style one. MEASURED 2026-09-24: a
# terminal's DECRPM reply (`ESC [ ? 2004 ; 2 $ y`) is not recognised by tmux's
# client key parser -- it consumes Device Attributes replies, which end in `c`,
# but not these, which end in `y` -- so the bytes arrive as KEYS. In
# choose-tree `?` opens the (search) prompt and a digit CHOOSES that entry, so
# one stray reply painted the status line yellow and yanked the client into an
# arbitrary session, every cold connect (#2140).
#
# Ruled out by measurement, not argument: draining the tty first (still
# stolen), hosting the chooser in a pane (prompt gone, still stolen), any
# recovery keypress (already switched), and upgrading tmux -- 3.7c was built
# and probed and is STILL stolen, and a pane's query never reaches the terminal
# on 3.4 anyway, so an upgrade removes no source.
#
# A line-based screen has no single-key actions, so stray bytes become part of
# a line that fails to parse. choose-tree is one keypress away (`t`, or Ctrl-b
# s) once the terminal has stopped talking.
# Resolve the picker beside THIS script. `${0%/*}` alone is wrong when $0
# carries no slash (invoked via PATH, an alias, or `sh lobby-door.sh`): the
# expansion leaves $0 untouched and yields `lobby-door.sh/lobby-picker.sh`.
# ssh always passes an absolute path, so the login route is safe either way --
# every manual and debug route is not, and the failure is the silent one below.
# shellcheck disable=SC1007  # `CDPATH= cd` is the idiom, not a typo'd assignment:
# it empties CDPATH for this one command so `cd` cannot resolve elsewhere or
# echo the target, either of which would corrupt the captured path.
_dir=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P) || _dir=""
PICKER="${_dir}/lobby-picker.sh"

# DEGRADE TO A SHELL, NEVER TO NO-LOGIN.
#
# MEASURED: with the picker absent, `new-session <cmd>` runs a command that
# fails instantly, the pane dies, the window closes, the session has no windows
# left, it is destroyed, the client detaches and ssh exits. The operator sees
# the terminal flash and print `[exited]`, with no error and no clue, on EVERY
# connect. The old door could not fail this way -- `new-session` with no command
# always left a shell, so even a broken chooser left you logged in.
#
# Causes are all live: the picker is a separate file that can go missing (it is
# deleted by `git clean -fd`, absent in a fresh clone or a worktree, and lost by
# any deploy that tidies untracked files), it can lose its executable bit across
# a copy, and `$0` can resolve wrong per the note above. None of that should
# cost the operator their one-click fleet access, so check first and fall back
# to exactly what the pre-picker door did.
# ONE exec, with the pane command as an optional argument, rather than an exec
# per branch. Two exec lines would create exactly one session at runtime (the
# branches are exclusive) but TWO textually, and `test_the_session_is_per_connection`
# counts attach lines to pin "one session per connection". That test is right to
# count: keeping the invariant checkable is worth more than the extra branch, and
# loosening the test to accept two would retire the check for every future edit.
if [ -x "$PICKER" ]; then
    # `/bin/sh` is passed as a SEPARATE argument, not folded into one string.
    # With a single shell-command argument tmux runs it through `sh -c`, which
    # word-splits -- so a repo path containing a space becomes two nonexistent
    # paths, the pane command fails, and this door's failure mode for that is a
    # LOGOUT: the window is destroyed, then the session, then the client is
    # detached. MEASURED on tmux 3.4 with the picker under a directory named
    # `dir with space`:
    #   new-session -d -s doorA "$PICKER"            -> session does NOT exist
    #   new-session -d -s doorB /bin/sh "$PICKER"    -> menu drawn
    # With two or more arguments tmux execs them directly, so nothing splits.
    # Latent on this install (no space in the path today) and one word to close;
    # the consequence is the exact failure the picker-missing branch below exists
    # to prevent, so it is not left to luck about where the repo is cloned.
    set -- /bin/sh "$PICKER"
else
    printf 'lobby: picker missing or not executable (%s) -- plain shell.\n' \
        "$PICKER" >&2
    set --
fi

exec tmux -u new-session -s "$SESSION" "$@" \; \
    set-option -t "=${SESSION}:" destroy-unattached on
