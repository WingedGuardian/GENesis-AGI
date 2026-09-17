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
# Idempotent and silent, MEASURED against a session holding live work under a
# stale chooser: three runs left pane_pid, pane_mode and pane_current_command
# byte-identical and printed nothing. (`new-session -d -A` would do the same job
# but prints "open terminal failed: not a terminal" every time, so the
# has-session form is used instead.)
WORKSPACE="lobby"
tmux has-session -t "=${WORKSPACE}" 2>/dev/null \
    || tmux new-session -d -s "$WORKSPACE" 2>/dev/null

# Record the fleet's pane-MODE state before the picker opens. Purely
# observational — it changes nothing and decides nothing, so the destroys-
# nothing guarantee above is untouched.
#
# The door fixed fact 1 for the LOBBY pane by making the picker per-connection.
# It does not reach the panes the picker SELECTS: a `cc-*` slot left in a mode
# (an operator pressed `Ctrl-b s` and disconnected) still holds it, and
# selecting that slot lands the next connection inside a stale chooser. That is
# the remaining path into the "frozen session with the yellow line", and it has
# never been captured live. See scripts/fleet_entry_capture.sh for the measured
# mechanism and for why no programmatic clear exists.
#
# Resolved from THIS script's own location, never "${HOME}/genesis" — the door
# must keep working on a clone that lives anywhere else.
#
# SYNCHRONOUS on purpose. Backgrounding would race the picker and record a
# snapshot of a fleet that had already moved, and an accurate snapshot is the
# entire value here. The cost is bounded: every tmux read inside carries a 5s
# belt, the typical cost is a sub-100ms local socket query, and the only state
# that could spend that belt — a wedged tmux server — would already have hung
# the `has-session` call three lines above. Failures are silent by construction.
# Split rather than inlined, and with an explicit `|| _fc_dir=""`: this door
# runs under `set -uo pipefail` today, but its sibling cc-slot.sh adds `-e`,
# where an assignment whose command substitution fails aborts before the `exec`
# and locks the operator out. Written the safe way in both, so the two doors
# cannot diverge on it later.
_fc_dir="$(cd "$(dirname "$0")" 2>/dev/null && pwd)" || _fc_dir=""
if [ -n "$_fc_dir" ] && [ -x "${_fc_dir}/fleet_entry_capture.sh" ]; then
    "${_fc_dir}/fleet_entry_capture.sh" lobby >/dev/null 2>&1 || true
fi

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
while tmux has-session -t "=${SESSION}" 2>/dev/null; do
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
