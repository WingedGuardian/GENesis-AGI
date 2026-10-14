# shellcheck shell=bash
# The runaway response for tmp_watchgod.sh: pause the command writing a
# runaway Claude Code task output, then empty the file once no writer of it
# is left running. Detection, and the decision to call this, live in
# scripts/lib/watchgod_runaway.sh (wg_runaway_check); this file only acts.
# Sourced after watchgod_signal.sh, watchgod_records.sh and
# watchgod_runaway.sh. Pure functions; nothing runs at source time.

# A command the guardian paused and nobody resumed is paged again every this
# many hours while it stays paused: nothing resumes it on its own, and a
# dispatched session has no one watching for the first page.
DG_RUNAWAY_REPAGE_H=6
# How long a poll waits for the pause records' lock. An operator's thaw or kill
# holds it for seconds at most; the response is retried on the next poll, so
# the poll loop never waits long (review finding: 5 s per file, twice per poll,
# while a kill held it).
DG_RUNAWAY_LOCK_WAIT_S=1
# Per recorded pause ("pid:start:paused-at"): reminders queued so far.
declare -gA _RW_REMINDED=()
# Action pages not yet in the alert queue, retried every poll (dedupe key ->
# "title<US>body"). Held in memory: on a full disk there is nowhere durable to
# hold them. A restart drops them; the records on disk are re-paged by
# _rw_repage.
declare -gA _RW_PENDING=()
# Set by _rw_respond for the detection page of the same poll.
_RW_PAUSED_NOW=0
_RW_ACTED=""
# 1 when _rw_respond emptied the file it was called for, this poll.
_RW_EMPTIED=0
# The last MB of an emptied output, for whoever investigates: owner-only, in an
# owner-only directory (the output can carry a credential).
DG_RUNAWAY_TAIL_DIR="$HOME/tmp/watchgod-truncated"

# ── The response: pause the command ─────────────────────────
# Only ONE kind of file is ever acted on: a Claude Code Bash command's
# output, cc-tmp/claude-<uid>/<project>/<session>/tasks/<id>.output. It is the
# tool's own transient record of a command's stdout, and the one file whose
# runaway has filled cc-tmp and broken every session's Bash. Every other file,
# on every filesystem, is page-only: a database is held open for writing by
# many processes, and pausing one of those stalls everything.
#
# The command is PAUSED (SIGSTOP), never killed, and never the session: the
# file is held by the command's own shell and its children, never by the
# `claude` process (MEASURED: fd 1 and 2 belong to the task's bash, parent
# claude). Every holder must descend from a claude process and not be one, or
# nothing is done. What is paused is the command's ROOT (the ancestor whose
# parent is that claude) and every holder, parents first: a root that closed
# its own descriptor could otherwise start the writer again.
#
# Identity: every process is the pair (pid, start time) read through one
# handle on its /proc entry (wg_proc.py: holders at discovery, chain for the
# ancestry). Every signal is bound to it (wg_signal_gen), and every record.
# A holder whose start time cannot be read is not acted on.
#
# Records (scripts/lib/watchgod_records.sh) follow one rule: the record is
# written BEFORE the signal, as "pending", and promoted to "paused" once the
# stop is seen. A record that cannot be written means no signal (review
# finding: on a full disk the record is what fails). The lock is waited for at
# most DG_RUNAWAY_LOCK_WAIT_S; when an operator's thaw or kill holds it, nothing
# is paused this poll. Once taken it is HELD from the first record through the
# re-walk, the tail copy and the truncate, so thaw cannot resume a writer
# between the check and the truncate. That hold is bounded by the timeouts
# inside it (the re-walk and the uninspectable-process walk, 10 s each; the
# tail copy 10 s; the truncate 5 s), about 35 s at worst; an operator's thaw
# waits for it (security review: the 1 s is the wait, not the hold). No child
# started under the lock inherits its descriptor ({lk}>&-): a child stuck in
# uninterruptible sleep would otherwise hold it, and thaw waits on it.

_rw_task_output() {
    # 0 when $1 is exactly a Claude Code task output under cc-tmp.
    local path="${1% (deleted)}" root rel
    # The path goes into the tab-separated pause record: a tab or newline in
    # it would split or forge a record line. No task output has either.
    [[ "$path" != *$'\t'* && "$path" != *$'\n'* ]] || return 1
    root="$(cd -P -- "$CC_TMP_DIR" 2>/dev/null && pwd -P)" || return 1
    [[ "$path" == "$root"/* ]] || return 1
    rel="${path#"$root"/}"
    local -a part
    IFS=/ read -r -a part <<< "$rel"
    (( ${#part[@]} == 5 )) || return 1
    [[ "${part[0]}" =~ ^claude-[0-9]+$ && -n "${part[1]}" && -n "${part[2]}" \
       && "${part[3]}" == tasks && "${part[4]}" == ?*.output ]]
}

_rw_state() {
    # The one-letter state of pid $1, or nothing when it is gone. The status
    # file escapes the name, so its lines cannot be forged by it.
    awk '/^State:/ { print $2; exit }' "${DG_PROC:-/proc}/$1/status" 2>/dev/null || true
}

_rw_stopped() {
    # 0 once pid $1 reports a stopped state while it is still the process that
    # started at $2, polling for up to a second. $3 "T": accept only T (the
    # process was in a tracing stop before, so t proves nothing).
    local i st
    for (( i = 0; i < 10; i++ )); do
        st="$(_rw_state "$1")"
        if [[ "$st" == T || ( "$st" == t && "${3:-}" != T ) ]]; then
            wg_same_gen "$1" "$2"
            return
        fi
        sleep 0.1
    done
    return 1
}

_rw_chain() {
    # Fill _RW_CH[pid] with wg_proc.py's chain line for each "pid start" on
    # stdin ("-": whatever holds the pid now). Fails when the read did not
    # complete: nothing is acted on without it.
    local out line
    _RW_CH=()
    out="$(timeout -k 2 "$DG_RUNAWAY_SCAN_TIMEOUT_S" python3 -I -S "$_RW_PROC_PY" "${DG_PROC:-/proc}" chain \
               2>/dev/null)" || return 1
    while IFS= read -r line; do
        [[ "$line" =~ ^[0-9]+$'\t' ]] && _RW_CH[${line%%$'\t'*}]="$line"
    done <<< "$out"
    return 0
}
declare -gA _RW_CH=()

_rw_blind() {
    # "#blind <pid>" for every live process whose descriptors this uid cannot
    # inspect: an fd directory it may not list, or entries whose link it may
    # not read or whose target cannot be stat'ed. Last line "#rc <status>"
    # (above 1: the walk did not complete). Run only when a file reached the
    # act gates: it is a second walk.
    local proc="${DG_PROC:-/proc}" d out rc=0
    for d in "$proc"/[0-9]*; do
        [[ -d "$d/fd" ]] || continue
        [[ -r "$d/fd" && -x "$d/fd" ]] || printf '#blind %s\n' "${d##*/}"
    done
    out="$(timeout -k 2 "$DG_RUNAWAY_SCAN_TIMEOUT_S" find "$proc"/[0-9]*/fd -mindepth 1 -maxdepth 1 \
               -printf '%h\t%Y\t%l\n' 2>/dev/null)" || rc=$?
    awk -F'\t' '$3 == "" || $2 == "?" || $2 == "L" {
        n = split($1, a, "/"); if (a[n] == "fd" && a[n - 1] ~ /^[0-9]+$/) print "#blind " a[n - 1]
    }' <<< "$out" | sort -u
    printf '#rc %s\n' "$rc"
    return 0
}

_rw_page_act() {
    # Queue the action page $2/$3 under dedupe key $1, or hold it for retry.
    if queue_alert_try emergency "watchgod:disk" "$2" "$3" "$1"; then
        unset "_RW_PENDING[$1]"
        return 0
    fi
    _RW_PENDING[$1]="$2"$'\x1f'"$3"
    log WARN "could not queue the runaway-response page (alert queue unwritable?); retrying every poll"
    return 0
}

_rw_flush_pending() {
    local k v
    for k in "${!_RW_PENDING[@]}"; do
        v="${_RW_PENDING[$k]}"
        queue_alert_try emergency "watchgod:disk" "${v%%$'\x1f'*}" "${v#*$'\x1f'}" "$k" \
            && { unset "_RW_PENDING[$k]"; log INFO "queued the held runaway-response page ${k}"; }
    done
    return 0
}

_rw_blind_veto() {
    # $1: the command roots ("pid:start" space-separated). Prints why acting is
    # not safe when a process in one of those commands cannot be inspected
    # (or the check did not complete), and succeeds; fails when it is safe.
    # A single-threaded zombie holds nothing.
    local roots=" $1 " blind brc="" b pid status state threads anc r in=""
    blind="$(_rw_blind)"
    while read -r b pid; do
        if [[ "$b" == "#rc" ]]; then brc="$pid"; continue; fi
        [[ "$b" == "#blind" && "$pid" =~ ^[0-9]+$ ]] && in+="${pid} -"$'\n'
    done <<< "$blind"
    if [[ ! "$brc" =~ ^[01]$ ]]; then
        printf 'the check for uninspectable processes did not complete'; return 0
    fi
    [[ -n "$in" ]] || return 1
    if ! _rw_chain <<< "$in"; then
        printf "the uninspectable processes' ancestry could not be read"; return 0
    fi
    for pid in "${!_RW_CH[@]}"; do
        IFS=$'\t' read -r _ status _ state threads _ _ anc <<< "${_RW_CH[$pid]}"
        [[ "$status" == gone || "$status" == nocc || "$status" == claude ]] && continue
        [[ "$state" == Z && "$threads" == 1 ]] && continue
        if [[ "$status" != ok ]]; then
            printf 'pid %s cannot be inspected and its ancestry cannot be read' "$pid"; return 0
        fi
        for r in $roots; do
            [[ ",${anc}," == *",${r},"* ]] && { printf 'pid %s, in the same command, cannot be inspected' "$pid"; return 0; }
        done
    done
    return 1
}

_rw_all_paused() {
    # 0 when every holder pid in $2.. is a process this guardian paused while
    # it wrote THIS file ($1, dev:inode): its identity (as read at discovery)
    # has a "paused" record for this file, and it is stopped. Only such a file
    # is emptied without new growth: a writer someone thawed has no record any
    # more, so it is never paused again on size alone.
    local key="$1" pid st recs
    shift
    (( $# )) || return 1
    recs="$(wg_records_list)" || return 1
    for pid in "$@"; do
        st="${_RW_IDENT[$pid]:-}"; st="${st%%$'\t'*}"
        [[ "$st" =~ ^[0-9]+$ ]] || return 1
        awk -F'\t' -v p="$pid" -v s="$st" -v k="$key" \
            '$1 == p && $2 == s && $5 == k && $7 == "paused" { f = 1 } END { exit !f }' <<< "$recs" || return 1
        [[ "$(_rw_state "$pid")" == T ]] || return 1
    done
    return 0
}

_rw_private_dir() {
    # 0 when $1 is a directory (not a symlink) owned by this uid with mode
    # 0700, creating it or tightening its mode first; failure otherwise.
    local d="$1" mode owner
    ( umask 077; mkdir -p -- "$d" ) 2>/dev/null || return 1
    [[ -d "$d" && ! -L "$d" ]] || return 1
    read -r mode owner < <(stat -c '%a %u' -- "$d" 2>/dev/null) || return 1
    [[ "$owner" == "$(id -u)" ]] || return 1
    if [[ "$mode" != 700 ]]; then
        chmod 700 -- "$d" 2>/dev/null || return 1
        [[ "$(stat -c %a -- "$d" 2>/dev/null)" == 700 ]] || return 1
    fi
    return 0
}

_rw_unpaused_holders() {
    # After the pause: every process that NOW holds $1 (dev:inode) open for
    # writing and is not one of the processes paused here ($2: "pid:start"
    # space-separated) in a stopped state, one pid per line. A holder at a pid
    # we paused but with another start time is a different process: running.
    # A forked child inherits the descriptor, and something else may have
    # opened the file since the walk: emptying it under a running writer lets
    # it refill at once. Prints "#incomplete" when the walk, or a re-check,
    # did not complete: then nothing can be confirmed.
    local key="$1" ours=" ${2:-} " k s pid fd _d rc="" id st state src
    local -A seen=()
    while read -r k s pid fd _d; do
        if [[ "$k" == "#rc" ]]; then rc="$s"; continue; fi
        [[ "$k" == "$key" && -n "$pid" && -z "${seen[$pid]:-}" ]] || continue
        # Still on this file through that descriptor? stat -L touches the
        # file's filesystem, so it is bounded.
        src=0
        id="$(timeout -k 1 2 stat -L -c %d:%i -- "${DG_PROC:-/proc}/$pid/fd/$fd" 2>/dev/null)" || src=$?
        case $src in
            0) ;;
            124|137) echo "#incomplete"; return 0 ;;
            # Gone (closed, or its process exited): not a holder. Still there
            # but not stat-able: a holder that cannot be checked.
            *) [[ -L "${DG_PROC:-/proc}/$pid/fd/$fd" ]] && { echo "#incomplete"; return 0; }
               continue ;;
        esac
        [[ "$id" == "$key" ]] || continue
        seen[$pid]=1
        st="$(wg_starttime "$pid" 2>/dev/null)" || { [[ -e "${DG_PROC:-/proc}/$pid" ]] && { echo "#incomplete"; return 0; }; continue; }
        state="$(_rw_state "$pid")"
        [[ "$ours" == *" ${pid}:${st} "* && ( "$state" == T || "$state" == t ) ]] && continue
        [[ -z "$state" ]] && continue   # exited
        printf '%s\n' "$pid"
    done < <(_rw_scan)
    [[ "$rc" =~ ^[01]$ ]] || echo "#incomplete"
    return 0
}

_rw_empty() {
    # Empty the task output $3 (dev:inode $2) through a descriptor of one of
    # the processes paused here ($4: "pid:start" space-separated), keeping its
    # last MB where there is room. $1: the domains, to judge the room. Sets
    # _RW_EMPTY_NOTE; succeeds only when the file was confirmed empty. Called
    # under the records lock, with the lock's descriptor closed for it.
    local domains="$1" key="$2" path="$3" ours="$4" p ffd flink="" ofd="" own="" tdom tkey dest=""
    _RW_EMPTY_NOTE="no tail kept"
    # Reach the file through a PAUSED holder (a stopped process cannot close
    # or reuse a descriptor), open it on a descriptor of our own, and act only
    # if that descriptor is still the file the walk saw: a /proc fd link of a
    # pid that exited and was recycled would name some other file.
    for p in $ours; do
        wg_same_gen "${p%%:*}" "${p#*:}" || continue
        for ffd in "${DG_PROC:-/proc}/${p%%:*}"/fd/*; do
            # readlink never touches the target's filesystem; only a link that
            # names this file is stat'ed, and under a timeout.
            [[ "$(readlink -- "$ffd" 2>/dev/null)" == "$path" ]] || continue
            [[ "$(timeout -k 1 2 stat -L -c %d:%i -- "$ffd" 2>/dev/null)" == "$key" ]] && { flink="$ffd"; break 2; }
        done
    done
    [[ -n "$flink" ]] || { _RW_EMPTY_NOTE="no paused writer still leads to it"; return 1; }
    { exec {ofd}>>"$flink"; } 2>/dev/null || { _RW_EMPTY_NOTE="it could not be opened"; return 1; }
    own="/proc/$BASHPID/fd/$ofd"
    if [[ "$(timeout -k 1 2 stat -L -c %d:%i -- "$own" 2>/dev/null)" != "$key" ]]; then
        exec {ofd}>&-
        _RW_EMPTY_NOTE="the file changed while it was opened"
        return 1
    fi
    # The tail only where there is room: the copy must never gate the
    # emptying, and never fill a second disk. mktemp creates the copy 0600
    # and exclusively under a name no other tail can take.
    tdom="$(_rw_domain_for "$(timeout -k 1 2 stat -L -c %d -- "$HOME/tmp" 2>/dev/null)" "$(wg_canon "$HOME/tmp")" "$domains")"
    tkey="${tdom%% *}"
    if [[ -z "$tkey" || ( "${RW_TIER[$tkey]:-}" != green && "${RW_TIER[$tkey]:-}" != yellow ) ]]; then
        _RW_EMPTY_NOTE="no tail kept: the disk holding ~/tmp has no room to spare"
    else
        if _rw_private_dir "$DG_RUNAWAY_TAIL_DIR"; then
            dest="$(umask 077; mktemp --suffix=.tail "$DG_RUNAWAY_TAIL_DIR/${path##*/}.$(date +%s).XXXXXXXX" 2>/dev/null)" || dest=""
        fi
        if [[ -n "$dest" ]] && timeout -k 2 10 tail -c 1048576 -- "$own" > "$dest" 2>/dev/null; then
            _RW_EMPTY_NOTE="last 1 MB kept in ${dest}"
        else
            [[ -n "$dest" ]] && rm -f -- "$dest" 2>/dev/null
            _RW_EMPTY_NOTE="the tail could not be kept"
        fi
    fi
    if timeout -k 1 5 truncate -s 0 -- "$own" 2>/dev/null \
            && [[ "$(timeout -k 1 2 stat -L -c %s -- "$own" 2>/dev/null)" == 0 ]]; then
        exec {ofd}>&-
        return 0
    fi
    exec {ofd}>&-
    _RW_EMPTY_NOTE="the truncate failed (${_RW_EMPTY_NOTE})"
    return 1
}

_rw_respond() {
    # Pause the command writing a runaway task output, then empty it once no
    # writer is left running. $1 generation, $2 dev:inode, $3 path, $4 size
    # MB, $5 the domains, rest: holder pids (identity in _RW_IDENT). Sets
    # _RW_PAUSED_NOW=1 when it paused something this poll, _RW_EMPTIED=1 when
    # it emptied the file, and _RW_ACTED to a one-line summary for the
    # detection page.
    local gen="$1" key="$2" path="$3" size_mb="$4" domains="$5"
    shift 5
    _RW_PAUSED_NOW=0; _RW_ACTED=""; _RW_EMPTIED=0
    local pid st ident status comm state threads depth root anc veto="" in="" r
    local -A st_of=() comm_of=() depth_of=() roots=()
    # Identity of every holder, as read at discovery.
    for pid in "$@"; do
        ident="${_RW_IDENT[$pid]:-}"
        st="${ident%%$'\t'*}"
        if [[ ! "$st" =~ ^[0-9]+$ ]]; then
            veto="pid ${pid}: its start time could not be read"; break
        fi
        st_of[$pid]="$st"; in+="${pid} ${st}"$'\n'
    done
    if [[ -z "$veto" ]] && ! _rw_chain <<< "$in"; then veto="its processes' ancestry could not be read"; fi
    if [[ -z "$veto" ]]; then
        for pid in "${!st_of[@]}"; do
            IFS=$'\t' read -r _ status comm state threads depth root anc <<< "${_RW_CH[$pid]:-$pid	unknown}"
            case "$status" in
                ok) depth_of[$pid]="$depth"; comm_of[$pid]="$comm"; roots[$root]=1 ;;
                gone) unset "st_of[$pid]" ;;   # exited since the walk: not a holder
                claude) veto="pid ${pid} is the Claude Code session itself"; break ;;
                nocc) veto="pid ${pid} holds it and is not a command of a Claude Code session"; break ;;
                *) veto="pid ${pid}: its ancestry could not be read"; break ;;
            esac
        done
    fi
    (( ${#st_of[@]} )) || veto="${veto:-its writers exited}"
    # The command roots: paused too, even when they hold no descriptor.
    if [[ -z "$veto" ]]; then
        in=""
        for r in "${!roots[@]}"; do in+="${r%%:*} ${r#*:}"$'\n'; done
        if ! _rw_chain <<< "$in"; then
            veto="its command's ancestry could not be read"
        else
            for r in "${!roots[@]}"; do
                pid="${r%%:*}"
                IFS=$'\t' read -r _ status comm _ _ depth _ _ <<< "${_RW_CH[$pid]:-$pid	unknown}"
                [[ "$status" == ok && "$depth" == 1 ]] || { veto="its command (pid ${pid}) changed while it was read"; break; }
                st_of[$pid]="${r#*:}"; comm_of[$pid]="$comm"; depth_of[$pid]=1
            done
        fi
    fi
    # A process in the command's tree whose descriptors cannot be inspected
    # may hold the file too (a setuid descendant, say): pausing the rest is
    # not a pause of the writer.
    if [[ -z "$veto" ]]; then
        veto="$(_rw_blind_veto "${!roots[*]}")" || veto=""
    fi
    if [[ -n "$veto" ]]; then
        _wg_warn_once "rw_veto_${gen}_${veto}" "runaway ${path}: not paused: ${veto}"
        _RW_ACTED="not paused: ${veto}"
        return 0
    fi

    local ts rstate pend rc paused="" newly="" unconfirmed="" refused="" prestopped="" order lk=""
    # Before the lock: nothing forked while it is held may inherit it.
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    # Parents first: a child stopped first can be reaped and replaced by its
    # still-running parent before the parent's turn.
    order="$(for pid in "${!depth_of[@]}"; do printf '%s %s\n' "${depth_of[$pid]}" "$pid"; done | sort -n)"
    if ! wg_records_lock lk "$DG_RUNAWAY_LOCK_WAIT_S"; then
        _wg_warn_once "rw_lock_${gen}" "runaway ${path}: not paused this poll: the pause records' lock could not be taken (an operator command holds it, or the state directory is not writable); retried every poll"
        _RW_ACTED="not paused this poll: the pause records' lock could not be taken (an operator command holds it, or the state directory is not writable)"
        return 0
    fi
    local deadline=$(( SECONDS + 5 )) tflag
    while read -r depth pid; do
        [[ "$pid" =~ ^[0-9]+$ ]] && (( pid > 1 )) || continue
        st="${st_of[$pid]}"
        rc=0; wg_same_gen "$pid" "$st" {lk}>&- || rc=$?
        (( rc == 1 )) && continue   # exited since it was read
        if (( rc != 0 )); then refused+=" ${pid}"; continue; fi
        state="$(_rw_state "$pid" {lk}>&-)"
        rstate="$(wg_record_state "$pid" "$st" {lk}>&- || true)"
        # Only T is a stop someone made; a recorded holder in t is ours (its
        # pending group-stop shows as t), except "pending-t": that holder was
        # in a tracing stop before our SIGSTOP, so only T confirms it (review
        # finding: it was promoted on t).
        if [[ "$state" == T || ( "$state" == t && ( "$rstate" == pending || "$rstate" == paused ) ) ]]; then
            if [[ "$rstate" == pending* ]]; then
                wg_record_promote "$pid" "$st" {lk}>&- || true
                paused+=" ${pid}"
            elif [[ "$rstate" == paused ]]; then
                paused+=" ${pid}"
            else
                prestopped+=" ${pid}"   # stopped by something else: left alone, never recorded
            fi
            continue
        fi
        if (( SECONDS >= deadline )); then refused+=" ${pid}"; continue; fi
        pend=pending; [[ "$state" == t ]] && pend=pending-t
        # The record first: no record, no signal.
        if ! wg_record_put "$pid" "$st" "${comm_of[$pid]:-?}" "$ts" "$key" "${path% (deleted)}" "$pend" {lk}>&-; then
            refused+=" ${pid}"; continue
        fi
        rc=0; wg_signal_gen STOP "$pid" "$st" {lk}>&- || rc=$?
        if (( rc != 0 )); then
            # Nothing was sent: the record would name a stop that never happened.
            wg_record_drop "$pid" "$st" {lk}>&- \
                || log WARN "runaway ${path}: the record of pid ${pid} names a stop that was never sent and could not be removed"
            (( rc == 1 )) || refused+=" ${pid}"
            continue
        fi
        tflag=""; [[ "$state" == t ]] && tflag=T
        if _rw_stopped "$pid" "$st" "$tflag" {lk}>&-; then
            wg_record_promote "$pid" "$st" {lk}>&- || true
            paused+=" ${pid}"; newly+=" ${pid}"
        else
            unconfirmed+=" ${pid}"
            [[ "$rstate" == pending* ]] || newly+=" ${pid}"
        fi
    done <<< "$order"

    # Empty the file only when NO writer is left running: every process here
    # confirmed stopped, and a fresh walk (start times as well as the file's
    # identity) finds no other running holder and no uninspectable process in
    # the command. Still under the lock, so thaw cannot resume a writer
    # between the check and the truncate.
    local why_not="" running="" ours="" q
    [[ -n "$unconfirmed$refused" ]] && why_not="not every writer is confirmed stopped"
    [[ -n "$paused" ]] || why_not="no writer was confirmed paused"
    if [[ -z "$why_not" ]]; then
        for q in $paused; do ours+=" ${q}:${st_of[$q]}"; done
        while read -r q; do
            [[ -n "$q" ]] || continue
            if [[ "$q" == "#incomplete" ]]; then why_not="the check for running writers did not complete"; continue; fi
            running+=" ${q}"
        done < <(_rw_unpaused_holders "$key" "${ours# }" {lk}>&-)
        [[ -n "$running" ]] && why_not="${why_not}${why_not:+; }still running (or stopped by something else, which can resume it): pid${running}"
    fi
    if [[ -z "$why_not" ]]; then
        q="$(_rw_blind_veto "${!roots[*]}" {lk}>&-)" && why_not="$q"
    fi
    if [[ -z "$why_not" ]]; then
        if _rw_empty "$domains" "$key" "$path" "${ours# }" {lk}>&-; then
            _RW_EMPTIED=1
            # The episode is over: a refill pages (and is acted on) again,
            # however long it takes.
            _rw_episode_open "$gen" "$(date +%s)"
        else
            why_not="$_RW_EMPTY_NOTE"
        fi
    fi
    wg_records_unlock "$lk"

    local all="${paused}${unconfirmed}"
    if [[ -z "$all" ]]; then
        log WARN "runaway ${path}: no writer could be paused (pid${refused:- none})"
        _RW_ACTED="no writer could be paused"
        return 0
    fi
    _RW_ACTED="paused"
    (( _RW_EMPTIED )) && _RW_ACTED="emptied"
    if [[ -z "$newly" ]] && (( ! _RW_EMPTIED )); then
        # Nothing new this poll: the reminders cover the pause.
        _wg_warn_once "rw_notempty_${gen}" "runaway ${path}: writers stay paused; the file could not be emptied${why_not:+ (${why_not})}"
        return 0
    fi
    [[ -n "$newly" ]] && _RW_PAUSED_NOW=1
    local sid body title p
    sid="$(_rw_session_of "${path% (deleted)}")"
    body="${path} (${size_mb} MB) was filling ${CC_TMP_DIR}."$'\n'
    [[ -n "$paused" ]] && body+="Paused (SIGSTOP, not killed): pid${paused}."$'\n'
    [[ -n "$unconfirmed" ]] && body+="SIGSTOP sent but NOT yet confirmed: pid${unconfirmed}. Recorded as pending; thaw resumes it and also cancels a stop that has not landed."$'\n'
    [[ -n "$refused" ]] && body+="Could not pause: pid${refused}."$'\n'
    [[ -n "$prestopped" ]] && body+="Already stopped by something else (not recorded, so thaw leaves it): pid${prestopped}."$'\n'
    body+="The Claude Code session itself was NOT paused; a tool call waiting on this command waits until it is resumed or until its own timeout. A paused command keeps any lock it holds."$'\n'
    if (( _RW_EMPTIED )); then
        body+="The output file was emptied (${_RW_EMPTY_NOTE})."$'\n'
    else
        body+="The output file was NOT emptied (${why_not:-unknown}): it keeps its space until it can be, or the command is resumed and finishes, or is ended."$'\n'
    fi
    [[ -n "$sid" ]] && body+="Claude Code session: ${sid}"$'\n'
    body+="Resume:"
    for p in $all; do body+=" scripts/watchgod thaw ${p};"; done
    body+=" or scripts/watchgod thaw all. A resumed command that is still growing fast while the disk is in trouble is paused again. End it instead:"
    for p in $all; do body+=" scripts/watchgod kill ${p};"; done
    if (( _RW_EMPTIED )) && [[ -z "$newly" ]]; then
        title="Emptied a runaway Claude Code task output (its writers were already paused)"
    elif (( _RW_EMPTIED )); then
        title="Paused a runaway Claude Code task and emptied its output"
    elif [[ -n "$paused" ]]; then
        # The title is what an alert listing shows: it must not claim the
        # recovery in the case that still needs someone.
        title="Paused a runaway Claude Code task; its output could NOT be emptied"
    else
        title="Sent SIGSTOP to a runaway Claude Code task, NOT yet confirmed; its output could NOT be emptied"
    fi
    log WARN "runaway ${path}: paused${paused:- none}; unconfirmed${unconfirmed:- none}; refused${refused:- none}; emptied=${_RW_EMPTIED}${why_not:+ (${why_not})}"
    _rw_page_act "watchgod:runaway-act:${key}:$(date +%s)" "$title" "$body"
    return 0
}

_rw_repage() {
    # Remind about every recorded pause still in place (same pid AND start
    # time), DG_RUNAWAY_REPAGE_H hours after ITS OWN pause and every period
    # after that. Promotes a "pending" record whose stop has landed first, under
    # the lock, so a file that stopped qualifying once its writers stopped is
    # still covered. _RW_REMINDED counts the reminders queued per pause; a
    # restart forgets it, so each pause older than a period gets one reminder
    # on the first poll, then resumes its own schedule.
    local now="$1" recs pid st comm ts _key path rstate t k g n=0 list="" pids="" due="" live note lk=""
    local period=$(( DG_RUNAWAY_REPAGE_H * 3600 ))
    local -A present=()
    if ! recs="$(wg_records_list)"; then
        _wg_warn_once rw_recs_unreadable "the pause records cannot be read; no paused command is reminded until they can"
        return 0
    fi
    [[ -n "$recs" ]] || { _RW_REMINDED=(); return 0; }
    # Under the lock: promote a pending record whose stop has landed, and drop
    # the record of a process that has exited (nothing is paused any more; it
    # would otherwise stay until the next boot).
    local rc
    if wg_records_lock lk "$DG_RUNAWAY_LOCK_WAIT_S"; then
        while IFS=$'\t' read -r pid st comm ts _key path rstate; do
            rc=0; wg_same_gen "$pid" "$st" {lk}>&- || rc=$?
            if (( rc == 1 )); then
                wg_record_drop "$pid" "$st" {lk}>&- && log INFO "pause record of pid ${pid} (${comm}) dropped: the process has exited"
                continue
            fi
            (( rc == 0 )) && [[ "$rstate" == pending* ]] || continue
            live="$(_rw_state "$pid" {lk}>&-)"
            [[ "$live" == T || ( "$live" == t && "$rstate" == pending ) ]] || continue
            wg_record_promote "$pid" "$st" {lk}>&- && log INFO "pause of pid ${pid} (${comm}) confirmed after the fact"
        done <<< "$recs"
        wg_records_unlock "$lk"
        if ! recs="$(wg_records_list)"; then
            _wg_warn_once rw_recs_unreadable "the pause records cannot be read; no paused command is reminded until they can"
            return 0
        fi
    fi
    while IFS=$'\t' read -r pid st comm ts _key path rstate; do
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        wg_same_gen "$pid" "$st" || continue
        live="$(_rw_state "$pid")"
        note=""
        if [[ "$rstate" == pending* ]]; then
            # Reminded whatever its state: a stop that never lands still needs
            # someone.
            note=" (SIGSTOP sent, not confirmed; state ${live:-?})"
        else
            [[ "$live" == T || "$live" == t ]] || continue
        fi
        g="${pid}:${st}:${ts}"
        present[$g]=1
        if ! t="$(date -d "$ts" +%s 2>/dev/null)"; then
            _wg_warn_once "rw_badts_${g}" "pause record of pid ${pid} has an unreadable time (${ts}); it is never reminded"
            continue
        fi
        k=$(( (now - t) / period ))
        (( k >= 1 && k > ${_RW_REMINDED[$g]:-0} )) || continue
        list+="  pid ${pid} (${comm}) paused since ${ts}${note}, writing ${path}"$'\n'
        pids+=" ${pid}"
        due+=" ${g}=${k}"
        n=$(( n + 1 ))
    done <<< "$recs"
    for g in "${!_RW_REMINDED[@]}"; do
        [[ -n "${present[$g]:-}" ]] || unset "_RW_REMINDED[$g]"
    done
    (( n )) || return 0
    local how="" p
    for p in $pids; do how+=" scripts/watchgod thaw ${p}; or end it: scripts/watchgod kill ${p}."; done
    if queue_alert_try warning "watchgod:disk" "Still paused: ${n} runaway Claude Code command(s)" \
            "${list}Nothing resumes them on their own. Resume all: scripts/watchgod thaw all. Each:${how}" \
            "watchgod:runaway-paused:$(printf '%s' "$due" | cksum | cut -d' ' -f1)"; then
        for g in $due; do _RW_REMINDED[${g%=*}]="${g##*=}"; done
    else
        log WARN "could not queue the still-paused page (alert queue unwritable?); retrying every poll"
    fi
    return 0
}
