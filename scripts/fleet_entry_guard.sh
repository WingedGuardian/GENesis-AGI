#!/usr/bin/env bash
# fleet_entry_guard.sh <label>
#
# Clears STALE TMUX PANE MODES on the fleet at the moment a connection enters,
# and records what it found to ~/.genesis/logs/fleet_entry_<YYYY-MM-DD>.log.
#
# WHY THIS EXISTS
# ---------------
# An operator intermittently lands in what looks like a frozen session: a row
# highlighted in yellow, keys doing nothing shell-like. The yellow is tmux's
# `mode-style` (default bg=yellow), NOT the status line — so the pane is in a
# MODE, normally `choose-tree` or `copy-mode`. The session is not frozen; it is
# sitting in a chooser and the keystrokes are chooser keys.
#
# A pane mode belongs to the PANE, not the client, so it outlives a disconnect.
# scripts/lobby-door.sh fixed that for the LOBBY pane by giving every connection
# its own throwaway picker. It never reaches the panes that picker SELECTS: a
# `cc-*` slot whose pane was left in a mode holds it indefinitely, and the next
# connection that selects it lands straight back inside the stale chooser.
# cc-slot.sh reaches the same hazard from the other side, because
# `new-session -A` attaches to an existing pane without clearing its mode.
#
# HOW IT CLEARS, AND THE NEGATIVE RESULT THAT WAS WRONG
# -----------------------------------------------------
# MEASURED 2026-09-17, tmux 3.4, isolated `-L` server, fresh-pane control at
# in_mode=0:
#
#   choose-tree on a cc-* pane, client attached  -> in_mode=1 mode=tree-mode
#   detach the client                            -> mode PERSISTS, attached=0
#   send-keys -X cancel  (attached AND detached) -> FAILS, "not in a mode",
#                                                   while pane_in_mode reads 1
#   send-keys q / send-keys Escape               -> both FAIL
#   copy-mode -q -t <pane_id>                    -> rc=0, in_mode 1 -> 0
#
# The first four arms are all `send-keys`, and an earlier revision of this file
# concluded from them that no programmatic clear existed. That was wrong: a
# negative across N variants of ONE command is not a negative across the
# capability space. `copy-mode -q` is a different command, documented to cancel
# copy mode *and any other mode*, and it is NON-DESTRUCTIVE — `pane_pid` and
# `pane_current_command` are byte-identical across the call, and it is a safe
# no-op on a pane that is not in a mode.
#
# Still UNMEASURED, and deliberately not depended on: whether a human pressing
# `q` at the keyboard escapes. Writing to a client pty exercises the OUTPUT side
# and cannot inject input, so that arm is inconclusive rather than negative.
#
# WHAT IT WILL AND WILL NOT TOUCH
# -------------------------------
# It clears a pane ONLY when all three hold:
#
#   1. the pane is in a mode                      (in_mode=1)
#   2. it is a SELECTABLE destination, not one of the doors' own transient
#      `lobby-<pid>` pickers, which are in tree-mode by design (kind=dest)
#   3. its session has NO client attached          (attached=0)
#
# Condition 3 is what keeps the doors' destroys-nothing contract intact. It is
# also exactly the bug's precondition — the mode survives a DISCONNECT — so it
# costs no coverage. A pane somebody is actively looking at is never touched,
# which means this can never yank a live client out of a selection it is part
# way through.
#
# What a clear costs, stated honestly rather than reassuringly:
#
#   - tree-mode / copy-mode: a VIEW POSITION only — a chooser cursor or a
#     scroll offset. The pane's process and scrollback are untouched, which is
#     the line lobby-door.sh draws when it refuses to respawn a pane.
#   - view-mode: the DISPLAYED TEXT IS LOST. MEASURED — output put in a pane by
#     `run-shell` lives in the mode, not in the scrollback, so after a clear it
#     is not recoverable by `capture-pane` at any history depth.
#
# That second case is a real cost and is accepted deliberately: the content is
# transient command output in a pane nobody is attached to, weighed against an
# operator who cannot use the slot at all. It is called out here, and in the
# operator docs, rather than buried under "only a view position" — which is
# what an earlier revision of this comment claimed, and it was wrong.
#
# Set GENESIS_FLEET_GUARD_CLEAR=off to disable clearing and keep the log only.
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

# ONE AGGREGATE DEADLINE for the whole run, not a per-call belt.
#
# The failure mode bounded is a WEDGED tmux server turning a diagnostic into a
# hung interactive login — the one hang this path cannot otherwise exclude. 5s
# is >50x the measured local query latency (sub-100ms for list-panes on this
# install), so it cannot clip a healthy read.
#
# A per-call belt does NOT bound a login. MEASURED: with three serial reads each
# carrying `timeout 5`, a stubbed hanging tmux made this script take 15s, not 5
# — the operator pays the SUM. Each call now gets only what is LEFT of the
# budget, and once it is spent the rest are skipped rather than started. Same
# defect class as a hook whose per-call timeouts cannot bound the aggregate
# against its registration.
#
# Pane data is read FIRST, so a partial budget still buys the field that
# matters; sessions and clients are context and are the right things to lose.
_GUARD_BUDGET_S=5
_guard_start="$(date +%s)"
_have_timeout=""
command -v timeout >/dev/null 2>&1 && _have_timeout=1

_tmux_do() {
    local remaining
    remaining=$(( _GUARD_BUDGET_S - ( $(date +%s) - _guard_start ) ))
    [ "$remaining" -gt 0 ] || return 0
    if [ -n "$_have_timeout" ]; then
        timeout "$remaining" tmux "$@" 2>/dev/null || true
    else
        # No `timeout` binary: the call is unbounded. Degrading to "do nothing"
        # would be worse — it would silently disable this on any install
        # lacking coreutils' timeout, which is the install least likely to
        # notice.
        tmux "$@" 2>/dev/null || true
    fi
}

# The pane format. Every DECISION field leads, and all of them are generated by
# tmux rather than by the operator: the in-mode flag, tmux's OWN verdict on
# whether this session is one of the doors' transient pickers, and the pane id.
#
# A session name is free text and may contain SPACES, so any parse that searches
# the whole line can be forged: a session named `x in_mode=1 mode=tree-mode y`
# would otherwise produce an ANOMALY while sitting at in_mode=0. Leading with
# fixed-width machine fields makes the parse unambiguous whatever the name
# contains — and the pane ID (`%N`) is what a clear TARGETS, for the same
# reason: `-t '=my scratch pane:'` is ambiguous where `-t %7` never is.
#
# The picker test uses `#{m:lobby-*,…}` — the SAME matcher lobby-door.sh filters
# its own chooser with — so the two cannot drift. Re-implementing that glob in
# shell is what invited the forgery in the first place.
_PANE_FORMAT='in_mode=#{pane_in_mode} kind=#{?#{m:lobby-*,#{session_name}},picker,dest} pane=#{pane_id} attached=#{session_attached} #{session_name}:#{window_index}.#{pane_index} mode=#{pane_mode} pid=#{pane_pid} cmd=#{pane_current_command}'

# INJECTABLE SEAM, for tests only. With GENESIS_FLEET_GUARD_PANES_FILE set to a
# readable file, pane lines are read from it instead of from tmux — so the
# CLASSIFICATION below can be exercised on a runner with no tmux at all, rather
# than hidden behind a skip guard that would make CI prove nothing. Clearing is
# suppressed under the seam: the pane ids in a fixture name nothing real, and a
# test must never issue a tmux command against whatever happens to be running.
_seamed=""
if [ -n "${GENESIS_FLEET_GUARD_PANES_FILE:-}" ] \
    && [ -r "${GENESIS_FLEET_GUARD_PANES_FILE}" ]; then
    panes="$(cat "${GENESIS_FLEET_GUARD_PANES_FILE}" 2>/dev/null)"
    sessions=""
    clients=""
    _seamed=1
else
    panes="$(_tmux_do list-panes -a -F "$_PANE_FORMAT")"
    sessions="$(_tmux_do list-sessions -F \
        '#{session_name} created=#{session_created} attached=#{session_attached}')"
    clients="$(_tmux_do list-clients -F '#{client_tty} session=#{client_session}')"
fi

# No server, or every read timed out: nothing to say. A cold first connection
# legitimately lands here, so this is silence rather than a record.
[ -n "$panes" ] || exit 0

# PASS 1 — every pane that is attached ANYWHERE.
#
# `list-panes -a` emits one row per (SESSION, pane), and `session_attached` is a
# property of the SESSION, not of the pane. A window linked into a second
# session — `link-window`, or a session group — therefore puts the SAME pane id
# on two rows with DIFFERENT attached counts. MEASURED: with a live client on
# `viewer`, pane %0 appears as `sess=owner attached=0 in_mode=1` AND
# `sess=viewer attached=1 in_mode=1`.
#
# Deciding per ROW would match that first line and clear a pane a client is
# displaying right now — the exact thing condition 3 exists to prevent. The
# attached test has to be aggregated over the pane, so build the set first.
attached_ids=" "
while IFS= read -r line; do
    [ -n "$line" ] || continue
    _r="${line#* }"; _r="${_r#* }"
    _f3="${_r%% *}"; _r="${_r#* }"
    _f4="${_r%% *}"
    [ "$_f4" = "attached=0" ] && continue
    attached_ids="${attached_ids}${_f3#pane=} "
done <<EOF
$panes
EOF

# PASS 2 — the STRANDED panes: in a mode, selectable, and attached NOWHERE. All
# three fields are tmux-generated and space-free, so this parse cannot be
# influenced by a session name.
stranded=""
stranded_ids=" "
while IFS= read -r line; do
    [ -n "$line" ] || continue
    _f1="${line%% *}"; _r="${line#* }"
    _f2="${_r%% *}";   _r="${_r#* }"
    _f3="${_r%% *}";   _r="${_r#* }"
    _f4="${_r%% *}"
    [ "$_f1" = "in_mode=1" ] || continue
    [ "$_f2" = "kind=dest" ] || continue
    [ "$_f4" = "attached=0" ] || continue
    _id="${_f3#pane=}"
    # Attached under some OTHER session — leave it alone.
    case "$attached_ids" in *" ${_id} "*) continue ;; esac
    # A shared pane also yields one stranded row per unattached session; record
    # it once. The surrounding spaces are what make this an exact-token test
    # rather than a prefix one (%1 must not match %10).
    case "$stranded_ids" in *" ${_id} "*) continue ;; esac
    stranded="${stranded}${_r#* }
"
    stranded_ids="${stranded_ids}${_id} "
done <<EOF
$panes
EOF
unset _f1 _f2 _f3 _f4 _r _id
[ "$stranded_ids" = " " ] && stranded_ids=""

# Clear them, then VERIFY. `copy-mode -q` returns 0 whether or not the pane was
# in a mode, so its exit status cannot distinguish "cleared" from "did nothing"
# — a check that can only ever confirm. One re-read after the sweep is what
# makes the log's claim honest, and a silently-failed clear (operator still
# stranded, log says fixed) is the worst outcome this could have.
cleared=""
still_stuck=""
incomplete=""

# THE OPERATOR LEVER, and where it has to be read FROM.
#
# An env var alone is unreachable here and the first version of this shipped
# exactly that mistake. MEASURED: an ssh RemoteCommand does not source .bashrc
# (this script's own header says so), sshd is `AcceptEnv LANG LC_*` with
# PermitUserEnvironment off, and lobby-door.sh sources nothing — so there was
# no way for an operator to set it. ~/.genesis/cc-slot.env is the file that
# already holds this install's door levers (cc-slot.sh sources it for
# GENESIS_CC_PERMISSION_MODE and the capacity tunables), so it is the lever
# surface both doors can actually be configured through.
#
# READ, never sourced. Sourcing executes the file on the login path; grepping
# one key cannot. That is the shape disk_hygiene.sh's `_load_store_knob` uses,
# for the same reason. Last assignment wins, as systemd reads these files.
_clear_lever="${GENESIS_FLEET_GUARD_CLEAR:-}"
if [ -z "$_clear_lever" ] && [ -f "${HOME}/.genesis/cc-slot.env" ]; then
    _line="$(grep -aE '^[[:space:]]*(export[[:space:]]+)?GENESIS_FLEET_GUARD_CLEAR=' \
        "${HOME}/.genesis/cc-slot.env" 2>/dev/null | tail -1)" || _line=""
    if [ -n "$_line" ]; then
        _clear_lever="${_line#*=}"
        case "$_clear_lever" in
            \"*\") _clear_lever="${_clear_lever#\"}"; _clear_lever="${_clear_lever%\"}" ;;
            \'*\') _clear_lever="${_clear_lever#\'}"; _clear_lever="${_clear_lever%\'}" ;;
        esac
    fi
    unset _line
fi

# Value semantics degrade toward LESS write authority: UNSET means on, because
# the feature has to work on a fresh install with no config — but any value that
# is SET and not a recognised enabling spelling turns clearing OFF. A typo
# (`GENESIS_FLEET_GUARD_CLEAR=flase`) therefore stops the mutation rather than
# silently permitting it, and `0`/`false`/`no` all disable as an operator would
# expect. Only exact-string "off" used to disable, which was the inverse.
_clear_enabled=1
if [ -n "$_clear_lever" ]; then
    case "$(printf '%s' "$_clear_lever" | tr '[:upper:]' '[:lower:]')" in
        on|1|true|yes) _clear_enabled=1 ;;
        *) _clear_enabled="" ;;
    esac
fi
unset _clear_lever

# Under the test seam the pane ids name nothing real, so no tmux command may be
# issued against whatever happens to be running.
_seam_suppressed=""
[ -n "$_seamed" ] && { _seam_suppressed=1; _clear_enabled=""; }

_budget_left() {
    [ "$(( _GUARD_BUDGET_S - ( $(date +%s) - _guard_start ) ))" -gt 0 ]
}

if [ -n "$_clear_enabled" ] && [ -n "$stranded_ids" ]; then
    # Track what was actually ATTEMPTED. `_tmux_do` returns silently once the
    # budget is spent, so without this a run that ran out of time mid-loop
    # reported nothing at all about the panes it never reached — three ANOMALY
    # lines and no CLEARED, CLEAR-FAILED or CLEAR-SKIPPED, which reads as "we
    # looked and did nothing" rather than "we ran out of time". MEASURED
    # against a stub sleeping 3s per clear.
    attempted=""
    for _id in $stranded_ids; do
        if ! _budget_left; then
            incomplete="${incomplete}${_id} "
            continue
        fi
        _tmux_do copy-mode -q -t "$_id"
        attempted="${attempted}${_id} "
    done
    unset _id

    if [ -n "$attempted" ] && _budget_left; then
        _after="$(_tmux_do list-panes -a -F 'pane=#{pane_id} in_mode=#{pane_in_mode}')"
        for _id in $attempted; do
            # The SPACE before `in_mode` is load-bearing: without it `%1` would
            # prefix-match `%10`. MEASURED both ways — with the space, %1 finds
            # no match in a blob holding only %10; without it, it matches.
            case "$_after" in
                *"pane=${_id} in_mode=1"*) still_stuck="${still_stuck}${_id} " ;;
                *"pane=${_id} in_mode=0"*) cleared="${cleared}${_id} " ;;
                # Neither: the pane vanished between the sweep and the re-read.
                # Not a clear and not a failure to clear — say nothing rather
                # than guess.
            esac
        done
        unset _id _after
    else
        # Cleared them but could not afford to look. Claiming success here is
        # exactly the confident lie the verification exists to prevent.
        incomplete="${incomplete}${attempted}"
    fi
    unset attempted
fi

{
    echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) entry=${label} pid=$$"
    if [ -n "$stranded" ]; then
        # Greppable: this is the line to search for after an operator reports a
        # yellow frozen session.
        printf '%s' "$stranded" | while IFS= read -r a; do
            [ -n "$a" ] && echo "ANOMALY moded-destination ${a}"
        done
    fi
    [ -n "$cleared" ] && echo "CLEARED ${cleared% }"
    [ -n "$still_stuck" ] && echo "CLEAR-FAILED ${still_stuck% }"
    [ -n "$incomplete" ] && echo "CLEAR-INCOMPLETE ${incomplete% } (budget spent)"
    # Two different reasons nothing was cleared, reported as two different
    # lines. Collapsing them would tell an operator their lever was in effect
    # when in fact a test seam was active, or the reverse.
    if [ -n "$stranded_ids" ] && [ -z "$_clear_enabled" ]; then
        if [ -n "$_seam_suppressed" ]; then
            echo "CLEAR-SKIPPED test seam active"
        else
            echo "CLEAR-SKIPPED disabled by GENESIS_FLEET_GUARD_CLEAR"
        fi
    fi
    echo "$panes" | sed 's/^/  pane /'
    [ -n "$sessions" ] && echo "$sessions" | sed 's/^/  sess /'
    [ -n "$clients" ] && echo "$clients" | sed 's/^/  client /'
} >>"$log_file" 2>/dev/null || exit 0

chmod 600 "$log_file" 2>/dev/null || true

exit 0
