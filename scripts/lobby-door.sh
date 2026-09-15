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
# So: reset ONLY an actual stale chooser, and give a concurrent window its own
# session instead of taking over.
set -uo pipefail

SESSION="lobby"
PRIMARY="lobby"
LOCK="${HOME}/.genesis/lobby-door.lock"

# ── Serialize the claim ──────────────────────────────────────────────────────
# Reading `session_attached` and then attaching is a TOCTOU: two SSH logins
# landing together both read 0, both keep SESSION=lobby, and the second's
# `choose-tree` drags the first into the chooser — recreating defect 2 in a
# narrow window. The whole check-and-claim runs under a lock.
#
# BOUNDED (-w 5) because this is the LOGIN path: a wedged holder must cost a
# racy login, never a hung one. Released explicitly BEFORE the exec below, so
# the lock never spans a tmux session — holding it across the attach would
# serialise every lobby login for as long as someone stayed connected.
#
# flock is not assumed present. Without it the claim is exactly as racy as it
# was before, which is a documented degradation rather than a silent one.
mkdir -p "${HOME}/.genesis" 2>/dev/null
_locked=0
if command -v flock >/dev/null 2>&1 && exec 9>"$LOCK" 2>/dev/null; then
    flock -w 5 9 2>/dev/null && _locked=1
fi

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
    state=$(tmux display-message -p -t "=${PRIMARY}:" \
        '#{session_attached}|#{pane_mode}' 2>/dev/null || printf '')
    attached=${state%%|*}
    pane_mode=${state#*|}
    # Non-numeric (tmux raced away, or an empty answer) -> treat as ATTACHED.
    # Fail direction is deliberate: guessing "free" would let this window take
    # over someone's work, which is the defect above. Guessing "busy" only
    # costs an extra ephemeral session.
    case "$attached" in
        ''|*[!0-9]*) attached=1 ;;
    esac

    # The OWNER marker. `$$` is the door process, and `exec tmux` below replaces
    # it in place — so this pid IS the tmux CLIENT and stays alive exactly as
    # long as that client is connected. It therefore closes the window between
    # the read above and the attach, which `session_attached` alone cannot: the
    # claimer has not attached yet, so it still reads 0. A dead pid means the
    # lobby is free again, and a recycled pid costs one ephemeral session.
    # The colon form AGAIN, and for the same reason. MEASURED: `set-option -t
    # =lobby` fails outright with "no such session" — so an earlier draft of this
    # claim recorded NOTHING while `2>/dev/null` hid the failure, leaving the
    # race it was written to close wide open and looking closed. The bare name
    # `lobby` happens to work only while an exact match exists; with the primary
    # gone it would prefix-match a concurrent `lobby-<pid>`. `=lobby:` resolves
    # and stays exact.
    owner=$(tmux show-options -qv -t "=${PRIMARY}:" @lobby_owner 2>/dev/null || printf '')
    owner_live=0
    case "$owner" in
        ''|*[!0-9]*) ;;
        *) kill -0 "$owner" 2>/dev/null && [ "$owner" != "$$" ] && owner_live=1 ;;
    esac

    if [ "$attached" -gt 0 ] || [ "$owner_live" = "1" ]; then
        # Somebody is in the lobby — do not touch it. This window gets its own
        # landing session, destroyed as soon as it is left, so these do not
        # accumulate. $$ keeps concurrent windows distinct.
        SESSION="lobby-$$"
    else
        if ! tmux set-option -t "=${PRIMARY}:" @lobby_owner "$$" 2>/dev/null; then
            # Not fatal, but not silent either: without the marker the claim
            # degrades to the pre-lock race rather than failing closed.
            printf 'lobby-door: could not record the lobby claim; a simultaneous open may share this pane.\n' >&2
        fi
        # RESET ONLY AN ACTUAL STALE CHOOSER. "Nobody is attached" is NOT the
        # same question: the operator uses this pane as a real command line, so
        # cancelling the picker, starting something long-running, and then
        # losing the SSH connection leaves a DETACHED pane with live work in it.
        # Respawning on detachment alone kills that work on the next reconnect —
        # the same loss this door exists to stop, arriving by the other door.
        #
        # MEASURED (tmux 3.4) — the three states are distinct, so the predicate
        # is exact rather than inferred:
        #     ordinary pane   in_mode=0  pane_mode=
        #     copy mode       in_mode=1  pane_mode=copy-mode
        #     stale chooser   in_mode=1  pane_mode=tree-mode
        # copy-mode is deliberately NOT reset: it holds a live process and a
        # scrollback selection, and attaching clears it with one keypress.
        #
        # An unreadable mode does NOT reset. That flips the earlier fail
        # direction on purpose: the cost of not resetting is one `q` once the
        # operator is attached (they have a client then, so the mode keys work),
        # and the cost of resetting wrongly is their work.
        if [ "$pane_mode" = "tree-mode" ]; then
            if ! tmux respawn-pane -k -t "=${PRIMARY}:" 2>/dev/null; then
                # Loud, not swallowed: a silent failure here is how the stale
                # chooser survives while everything LOOKS fine. Still non-fatal —
                # a usable lobby beats no lobby.
                printf 'lobby-door: could not reset the lobby pane; it may still show the previous chooser (press q).\n' >&2
            fi
        fi
    fi
fi

# Release before the exec: see the bound above.
[ "$_locked" = "1" ] && exec 9>&-

if [ "$SESSION" = "$PRIMARY" ]; then
    exec tmux -u new-session -A -s "$SESSION" \; choose-tree -Zs
fi

# Ephemeral secondary: `destroy-unattached` is set ON THE SESSION (-t), never
# globally — a global set would reap the cc-* slots the moment their terminal
# window closed, which is the exact opposite of why they exist.
exec tmux -u new-session -A -s "$SESSION" \; \
    set-option -t "$SESSION" destroy-unattached on \; \
    choose-tree -Zs
