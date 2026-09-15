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

# Per-connection, so two windows can never meet. One pid cannot open two doors,
# so a collision is not possible rather than merely unlikely.
SESSION="lobby-$$"

# `destroy-unattached` is set ON THIS SESSION (-t), never globally: a global set
# would reap every cc-* slot the moment its terminal window closed, which is the
# exact opposite of why the slots exist. It is set AFTER the attach, deliberately
# — MEASURED: setting it on a still-detached session destroys that session
# immediately, before any client can arrive.
exec tmux -u new-session -A -s "$SESSION" \; \
    set-option -t "=${SESSION}:" destroy-unattached on \; \
    choose-tree -Zs
