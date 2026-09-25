#!/bin/sh
# Fleet picker — the landing screen for the lobby door.
#
# WHY THIS EXISTS INSTEAD OF LANDING STRAIGHT IN choose-tree
# ----------------------------------------------------------
# MEASURED 2026-09-24. A terminal that answers a DECRQM query -- e.g.
# `ESC [ ? 2004 ; 2 $ y` for bracketed-paste status -- has those bytes spilled
# back out as KEYSTROKES by tmux's client key parser. tmux special-cases Device
# Attributes replies (final byte `c`) and consumes them; it does not recognise
# DECRPM replies (final byte `y`), so the unmatched bytes arrive as ordinary
# keys. Inside choose-tree that is not cosmetic:
#
#   `?`     opens the (search) prompt  -> the "yellow bar"
#   a digit CHOOSES that list entry    -> yanks the client into a session
#
# So one stray reply both painted the status line and stole the selection, and
# the keypress meant to dismiss the bar confirmed it. It always landed in the
# same session, which made it look like a cursor bug rather than an input one.
#
# RULED OUT BY MEASUREMENT, NOT BY ARGUMENT
#   - draining the tty before attach         -> still stolen
#   - hosting choose-tree inside a pane      -> prompt gone, still stolen
#   - pressing Escape / Enter / q to recover -> already switched by then
#   - upgrading tmux: 3.7c (latest stable) was BUILT and re-probed and is still
#     stolen; and a pane's DECRQM never reaches the terminal on 3.4 either, so
#     an upgrade removes no query source. It would have cost a server restart
#     and fixed nothing.
#
# The defence here is structural rather than a timing guess: this screen has NO
# single-key actions. It reads a LINE. Stray bytes become part of a line, the
# line fails to parse, and the menu redraws. The worst a leak can do is blink.
#
# choose-tree is NOT gone -- `t` opens it, and Ctrl-b s always has. It simply is
# not the thing sitting exposed while the terminal is still talking.
#
# NOTE the residual, deliberately: once `t` opens the tree, the operator IS in
# the vulnerable state again, and tree mode's keys include `x` (kill session,
# confirmed with `y`). That is a conscious keypress rather than the default
# landing, which is the whole trade -- but it is not zero, so it is written down
# rather than implied away.

# `-f` disables PATHNAME EXPANSION for the whole script, and it is load-bearing
# rather than tidiness. The row split below is an unquoted expansion -- that is
# how it splits on newlines -- and an unquoted expansion GLOBS as well as splits.
# MEASURED: with a session named `*` and two files in the pane's directory, the
# split turned one row into TWO rows named after those files, the real session
# vanished from the menu, and selecting a phantom row would have asked tmux to
# switch to a session that does not exist. Fabricated rows on the one screen
# whose job is to say truthfully what exists. tmux does permit that name, and
# `-t '=*'` targets it exactly, so this is reachable rather than theoretical.
#
# Nothing here wants pathname expansion. `case` patterns are unaffected -- they
# are pattern matching, not globbing -- so the whole script is safer with it off
# than one site would be with a local `set -f`/`set +f` pair somebody later
# moves code out from between.
set -uf

# Every "drop to a shell" path goes through here. A failed `exec` EXITS the
# shell -- MEASURED under dash, rc=127 for a missing target and 126 for a
# non-executable one, with the next line never reached -- and in this pane an
# exit destroys the window, then the session (destroy-unattached), then detaches
# the client. That is a LOGOUT from a prompt that offered a shell.
#
# Unreachable through the door, which is why this is one guard and not a
# redesign: MEASURED, tmux overwrites SHELL in a pane with the validated
# `default-shell` option, and REFUSES to set that option to a missing or
# non-executable path ("not a suitable shell", rc=1). But this script is also
# run by hand and in debug, where $SHELL is whatever the invoker has. Note
# `exec ... || fallback` does NOT work: exec failure exits before `||` is
# reached.
_login_shell() {
    _sh=${SHELL:-/bin/sh}
    [ -x "$_sh" ] || _sh=/bin/sh
    exec "$_sh" -l
}

# Every tmux request this script makes, with a wall-clock bound.
#
# This screen already states that a wedged server is possible -- it has a whole
# branch for "Cannot reach tmux" -- and then bounded only the `pane_in_mode`
# poll. A server that ACCEPTS a request and stops replying would block the
# others forever: the refresh, the tree, and the switch. The operator's door
# hangs with no menu, no error and no way out but killing the process, which is
# strictly worse than the failure the retry branch was written to handle.
#
# 5s, not the poll's 2s: these are one-shot requests on a healthy server
# (MEASURED ~7ms) and a slow-but-alive server should not be declared dead over a
# hiccup. `timeout` exits 124 on expiry, which every caller already treats as
# failure -- the retry screen, or the "is gone" message.
_tmux() {
    timeout 5 tmux "$@"
}

# A tmux that is not there, or a query that fails, must never read as "no
# sessions" -- that tells the operator their fleet is empty when it is running.
# Distinguish the two.
_sessions() {
    # `attached name`: attached is 0/1 and comes FIRST, so the split below is
    # safe even for a session name containing spaces (tmux permits them).
    # MEASURED against a real server: a session named `my scratch pad` lists as
    # one row and selecting it switches to that exact name.
    #
    # The `-f` expression is tmux's own, and it is the SAME one the `t` branch
    # hands choose-tree. It used to be a `grep -v ' lobby-'` on the rendered
    # line here, which is a second definition of the same set -- and a wrong
    # one: grep matches ANYWHERE in the line, so MEASURED on tmux 3.4 it hid
    # `my lobby-notes` and `cc-9 lobby-x` while the tree showed them. Sessions
    # the operator owns, invisible in the menu and unreachable by number, with
    # the two halves of one screen disagreeing about what exists.
    _tmux list-sessions -f '#{!=:#{m:lobby-*,#{session_name}},1}' \
                        -F '#{session_attached} #{session_name}' 2>/dev/null
}

# CANNOT FIRE in production, and kept anyway. This comment used to claim a
# session name "can carry control characters or escape sequences"; MEASURED
# FALSE on tmux 3.4 -- tmux vis-encodes the name at creation AND at rename, so a
# stored name can never contain a raw byte in \001-\037 or \177. Creating a
# session named with a literal newline stores and renders `alpha\n0 base` as
# thirteen PRINTABLE characters, and two such sessions still list as two rows.
# So `tr -d` here can never remove a byte, and the row-splitting hazard this was
# partly written for does not exist.
#
# It stays because this screen should not DEPEND on that guarantee: it costs
# nothing, and a screen whose entire job is terminal-byte hygiene should not be
# the one place trusting an upstream encoder. Residual, unchecked: raw C1 bytes
# (\200-\237), which this range would not catch either way.
_safe() {
    printf '%s' "$1" | tr -d '\001-\037\177'
}

while :; do
    raw=$(_sessions)
    rc=$?

    if [ "$rc" -ne 0 ]; then
        printf '\n  Cannot reach tmux (list-sessions failed).\n'
        printf '  Enter to retry, q for a shell: '
        read -r reply || _login_shell
        [ "$reply" = "q" ] && _login_shell
        continue
    fi

    # The throwaway per-connection pickers (lobby-<pid>) are already filtered
    # out server-side by _sessions, so there is nothing to strip here. An empty
    # result now also covers "every session on the server is a throwaway", which
    # is reachable: MEASURED, a server whose only session is `lobby-777` lands
    # here, i.e. the state where the door's own `lobby` was killed.
    list=$raw

    if [ -z "$list" ]; then
        printf '\n  No sessions yet.\n'
        printf '  Enter to refresh, q for a shell: '
        read -r reply || _login_shell
        [ "$reply" = "q" ] && _login_shell
        continue
    fi

    # Positional parameters, not a temp file. The file version leaked one per
    # successful connect: the happy path ends in SIGHUP (pick a session ->
    # switch-client -> the picker session becomes unattached -> destroy-unattached
    # kills the pane), and an EXIT trap does NOT run on SIGHUP -- MEASURED under
    # /bin/sh here, rc=129 with the trap never firing. No file, no trap, no leak,
    # and no mktemp failure mode to fall over on either.
    OLDIFS=$IFS
    IFS='
'
    # shellcheck disable=SC2086  # deliberate split on newlines only, IFS is set
    set -- $list
    IFS=$OLDIFS

    printf '\033[H\033[2J'
    printf '  Genesis Fleet\n\n'
    n=0
    for entry in "$@"; do
        n=$((n + 1))
        att=${entry%% *}
        name=${entry#* }
        mark='  '
        [ "$att" != "0" ] && mark=' *'
        printf '  %2d)%s %s\n' "$n" "$mark" "$(_safe "$name")"
    done
    printf '\n  * attached elsewhere\n'
    printf '\n  number to enter - t for the tree - r refresh - q shell: '

    # Reading a LINE is the whole defence. Nothing here acts on one byte.
    # EOF (^D) drops to a shell rather than dropping the ssh session: the pane
    # command exiting would destroy the window, then the session, then detach
    # the client -- so `exit` here logs the operator OUT, which is not what a
    # prompt offering "q shell" should do.
    read -r reply || _login_shell

    case "$reply" in
        q | Q) _login_shell ;;
        t | T)
            if ! _tmux choose-tree -Zs \
                       -f '#{!=:#{m:lobby-*,#{session_name}},1}' 2>/dev/null; then
                # Do not swallow it, for the same reason the failed switch below
                # does not: the operator pressed a key and something has to
                # explain why nothing happened.
                printf '\n  Could not open the session tree. Enter to refresh: '
                read -r _ || _login_shell
                continue
            fi
            # While a pane is in a mode tmux routes keys to the mode, so the
            # `read` above would block anyway; this wait exists only so the menu
            # does not repaint OVER a live chooser.
            #
            # Bounded, and bounded in SECONDS. The previous version counted 3600
            # iterations of `sleep 0.5`, which is two different mistakes. It was
            # 30 MINUTES, not the "safety net" it read as (MEASURED: ~7ms per
            # round trip, so 3600 x 0.507s ~ 1825s) -- long enough that a stuck
            # flag is an outage rather than a blip. And counting ITERATIONS
            # bounds nothing when the round trip itself can block: a wedged
            # server leaves `tmux display-message` hanging with no timeout and
            # the counter never advances, so the budget meant to bound the wait
            # sat outside the call that was actually stuck. `timeout` closes that,
            # and the elapsed check is the bound that was intended.
            #
            # The justification also did not survive checking. This cited
            # lobby-door.sh's note about `pane_in_mode` reading 1 after a
            # cancelled chooser -- but that note's measurement is `send-keys -X
            # cancel` and a DETACHED client, and it says outright that a human
            # pressing `q` was not measured. MEASURED now: `q` and `Escape` both
            # clear the mode within one poll interval, `-X cancel` is a path this
            # script never takes, and a detach on the picker's session means
            # destroy-unattached has already killed it. The bound is still worth
            # having -- it just protects against a state nobody has produced,
            # which is an argument for 60 seconds, not 30 minutes.
            _deadline=$(( $(date +%s) + 60 ))
            while [ "$(timeout 2 tmux display-message -p '#{pane_in_mode}' \
                            2>/dev/null)" = "1" ] \
                  && [ "$(date +%s)" -lt "$_deadline" ]; do
                sleep 0.5
            done
            continue
            ;;
        r | R | '') continue ;;
        *[!0-9]*) continue ;;   # stray bytes land here: redraw, act on nothing
    esac

    [ "$reply" -ge 1 ] 2>/dev/null || continue
    [ "$reply" -le "$n" ] 2>/dev/null || continue

    # Walk to the Nth entry rather than `eval "target=\${$reply}"`: eval on a
    # value derived from terminal input is the wrong reflex on this screen even
    # when the value is already proven to be digits, and shellcheck cannot see
    # through it either (SC2154), which costs the next reader a silenced warning.
    i=0
    target=''
    for entry in "$@"; do
        i=$((i + 1))
        if [ "$i" -eq "$reply" ]; then
            target=$entry
            break
        fi
    done
    name=${target#* }
    [ -n "$name" ] || continue

    if ! _tmux switch-client -t "=${name}" 2>/dev/null; then
        # Do not swallow it: the operator pressed a number and something has to
        # explain why nothing happened. Usually the session died since the last
        # redraw.
        printf '\n  "%s" is gone. Enter to refresh: ' "$(_safe "$name")"
        read -r _ || _login_shell
        continue
    fi
    exit 0
done
