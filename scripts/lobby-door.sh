#!/usr/bin/env bash
# The LOBBY door: a stable landing session that sees every live cc-* slot.
#
# Invoked from the generated ssh_config as the RemoteCommand for
# `<host>-lobby` (see scripts/generate-ssh-config.sh). It is a script rather
# than an inline `tmux ... \; ...` chain for the same reason the numeric slot
# door is one: the inline form has to survive ssh_config -> remote shell ->
# tmux quoting, which is the documented failure mode of that block, and a
# script can be tested.
#
# Two things it gets right that a bare `new-session -A -s lobby \; choose-tree`
# does not, both MEASURED on a live install:
#
# 1. A STALE CHOOSER IS NOT INHERITED. A tmux pane mode belongs to the PANE,
#    not the client, so `choose-tree` outlives the client that opened it: the
#    lobby pane was found at `in_mode=1 mode=tree-mode` with `attached=0`.
#    Reconnecting then landed INSIDE the previous chooser — another session's
#    preview on screen, keystrokes going to the chooser, tree-mode's search
#    prompt in the status line. Re-issuing choose-tree does not reset it, and
#    neither does `send-keys -X cancel` / `q` / `Escape` (mode keys dispatch
#    through a CLIENT's key table, and a stale pane has no client). Only
#    respawning the pane clears it.
#
# 2. A SECOND WINDOW DOES NOT STEAL THE FIRST. tmux sessions are shared: a
#    second client attaching to `lobby` gets the SAME pane, so opening another
#    Fleet window would reset the pane under the first window and drag both
#    into the chooser. That is not hypothetical — it killed a live codex the
#    operator was running in the lobby pane. An earlier revision of this door
#    dismissed that as "acceptable for a switchboard"; the operator uses the
#    lobby as a real command line, so it is not.
#
# So: reset ONLY when nobody is attached, and give a concurrent window its own
# session instead of taking over.
set -uo pipefail

SESSION="lobby"
PRIMARY="lobby"

if tmux has-session -t "=${PRIMARY}" 2>/dev/null; then
    # NOTE THE TRAILING COLON, it is load-bearing. `=NAME` is the exact-match
    # form for a SESSION target (has-session takes it), but display-message and
    # respawn-pane resolve a PANE target, where `=lobby` is not a session
    # qualifier at all. MEASURED: `display-message -p -t =lobby` returns rc=0
    # with EMPTY output (no error), and `respawn-pane -t =lobby` fails with
    # "can't find pane: =lobby". `=lobby:` names the session's current window
    # and resolves correctly for both. The colon also keeps the match EXACT, so
    # a concurrent `lobby-<pid>` below can never be hit by prefix bleed
    # (verified: respawning `=lobby:` left `lobby-99999`'s pane pid unchanged).
    # An empty read from the `=lobby` form is what made an earlier revision of
    # this script silently take the secondary path on EVERY connect, so the
    # reset never ran while everything looked correct.
    attached=$(tmux display-message -p -t "=${PRIMARY}:" '#{session_attached}' 2>/dev/null || printf '')
    # Non-numeric (tmux raced away, or an empty answer) -> treat as ATTACHED.
    # Fail direction is deliberate: guessing "free" would let this window take
    # over someone's work, which is the defect above. Guessing "busy" only
    # costs an extra ephemeral session.
    case "$attached" in
        ''|*[!0-9]*) attached=1 ;;
    esac
    if [ "$attached" -gt 0 ]; then
        # Somebody is in the lobby — do not touch it. This window gets its own
        # landing session, destroyed as soon as it is left, so these do not
        # accumulate. $$ keeps concurrent windows distinct.
        SESSION="lobby-$$"
    else
        # Nobody home: clear any chooser the last visit left behind. The
        # cc-* slots are a different target and are never touched.
        if ! tmux respawn-pane -k -t "=${PRIMARY}:" 2>/dev/null; then
            # Loud, not swallowed: a silent failure here is how the stale
            # chooser survives while everything LOOKS fine. Still non-fatal —
            # a usable lobby beats no lobby.
            printf 'lobby-door: could not reset the lobby pane; it may still show the previous chooser (press q).\n' >&2
        fi
    fi
fi

if [ "$SESSION" = "$PRIMARY" ]; then
    exec tmux -u new-session -A -s "$SESSION" \; choose-tree -Zs
fi

# Ephemeral secondary: `destroy-unattached` is set ON THE SESSION (-t), never
# globally — a global set would reap the cc-* slots the moment their terminal
# window closed, which is the exact opposite of why they exist.
exec tmux -u new-session -A -s "$SESSION" \; \
    set-option -t "$SESSION" destroy-unattached on \; \
    choose-tree -Zs
