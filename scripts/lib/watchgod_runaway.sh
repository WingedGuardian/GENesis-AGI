# shellcheck shell=bash
# Runaway-file detection for tmp_watchgod.sh.
#
# A disk does not fill on its own: some process is writing a file. The tiers
# in tmp_watchgod.sh see the free space fall; this file names the FILE and its
# WRITERS while they are still writing, on every watched filesystem.
#
# Why this exists. A Claude Code background task once wrote a 1.7 GB output file
# into the cc-tmp volume: a recursive grep whose search tree included that
# output, so it read its own output and wrote it out again. The volume's quota
# stopped it, every session's Bash failed with EDQUOT, and the daemon's writer
# attribution named the wrong process. /proc/<pid>/io charged the bytes to the
# session's `claude` process, while the descriptor holding the file belonged to
# the command's own shell. So the writer is taken from who holds the file open,
# never from I/O counters.
#
# It DETECTS and PAGES on every watched filesystem. It ACTS in one case only:
# a Claude Code Bash command's tasks/*.output in cc-tmp, while cc-tmp is ORANGE
# or RED and the guardian is in act mode. Then it pauses that command's
# processes (SIGSTOP; never the session, never a kill) and empties the file.
# `scripts/watchgod thaw` resumes them. Nothing else is stopped or changed.
#
# One poll costs one size-filtered walk of /proc/*/fd. A file is followed only
# when it is held open by a process this uid can see and is at least
# DG_RUNAWAY_MIN_MB, and fdinfo is read only for those candidates. MEASURED on
# a live install: 30 ms for the filtered walk against 297 ms for reading every
# descriptor's fdinfo. Files are keyed by device:inode, not by path. A file
# deleted while still held keeps using space, and its path no longer names it.
#
# A file is RUNAWAY when either holds:
#   * size >= DG_RUNAWAY_PCT % of its domain's total, or
#   * it grows at >= DG_RUNAWAY_RATE_PCT % of that total per POLL_INTERVAL.
# Growth is normalised to POLL_INTERVAL, in bytes, not measured per poll: the
# fast poll is 6x shorter, and a per-poll test would see one sixth of the same
# writer's growth there, drop out of fast mode, and flap (review finding).
# Growth needs two sightings, so a file is never "fast" on its first poll.
# Growth of at least half the rate threshold asks for the fast poll interval.
#
# An incident's identity is the FILE's own: device, inode and birth time
# (stat %W; 0 where the filesystem does not record it). It is derived on every
# poll, never remembered, so a restart re-sends the same dedupe key, which the
# alert drainer collapses, and a new file always has a new key, even where its
# inode number is reused at once. A file pages once per mode while it stays in
# sight; out of sight for a poll, it may be queued again under the same key.
#
# Pure functions; nothing runs at source time. DG_PROC (default /proc) lets the
# tests point the walk at a fake process table.

DG_RUNAWAY_PCT=25
DG_RUNAWAY_RATE_PCT=5
DG_RUNAWAY_MIN_MB=50
# Bound on the /proc walk: it stats every held file, and one on a hung network
# mount must not stall the poll loop (OOM capture included). -k: a find stuck
# in uninterruptible sleep ignores the TERM.
DG_RUNAWAY_SCAN_TIMEOUT_S=10
# A page lists at most this many writers, then "+N more".
DG_RUNAWAY_MAX_HOLDERS=20
# A command the guardian paused and nobody resumed is paged again every this
# many hours while it stays paused: nothing resumes it on its own, and a
# dispatched session has no one watching for the first page.
DG_RUNAWAY_REPAGE_H=6
_RW_REPAGED_PERIOD=-1

# Per file (device:inode): its size at the previous poll, and the modes it has
# been paged in while continuously in sight.
_RW_EMPTIED=0
declare -gA _RW_PREV=() _RW_PAGED=()
_RW_PREV_T=0
# Set by wg_runaway_check: 1 when some file grew fast enough to want 5 s polls.
# shellcheck disable=SC2034  # read by check_disks in tmp_watchgod.sh
RUNAWAY_FAST=0

_rw_scan() {
    # One line per descriptor held open FOR WRITING on a regular file of at
    # least DG_RUNAWAY_MIN_MB: "<dev:ino> <bytes> <pid> <fd> <dev>".
    # The last line is "#rc <status>": anything above 1 means the walk did not
    # complete, so the caller says so instead of reading silence as "no
    # runaway file".
    local proc="${DG_PROC:-/proc}" key size path pid fd flags rest listing rc=0
    # find exits 1 on the unreadable descriptors of other users' processes,
    # which is normal. Anything above 1 (a timeout's 124, or 125-127 and a
    # signal) means the walk did not complete. A killed find also loses output
    # it had found but not yet flushed, so a timed-out walk may miss files it
    # had already reached.
    listing="$(timeout -k 2 "$DG_RUNAWAY_SCAN_TIMEOUT_S" find -L "$proc"/[0-9]*/fd -mindepth 1 -maxdepth 1 -type f \
                   -size +"$(( DG_RUNAWAY_MIN_MB - 1 ))"M -printf '%D:%i %s %p\n' 2>/dev/null)" || rc=$?
    while read -r key size path; do
        [[ -n "$key" ]] || continue
        [[ "$path" =~ /([0-9]+)/fd/([0-9]+)$ ]] || continue
        pid="${BASH_REMATCH[1]}"; fd="${BASH_REMATCH[2]}"
        flags=""
        # fdinfo's flags are octal. Read access only (O_RDONLY: the low two
        # bits are 0) is not a writer, whatever the file is doing.
        while read -r rest; do
            [[ "$rest" == flags:* ]] && { flags="${rest#flags:}"; flags="${flags//[[:space:]]/}"; break; }
        done < "$proc/$pid/fdinfo/$fd" 2>/dev/null || true
        [[ "$flags" =~ ^[0-7]+$ ]] || continue
        (( (8#$flags & 3) != 0 )) || continue
        printf '%s %s %s %s %s\n' "$key" "$size" "$pid" "$fd" "${key%%:*}"
    done <<< "$listing"
    # The status rides the same stdout as the data: a status file would fail to
    # write on a full disk, which is exactly when this must still work.
    printf '#rc %s\n' "$rc"
    return 0
}

_rw_domain_for() {
    # $1 device number, $2 file path, $3 the domains ("key dev total tier path"
    # lines). Prints "key total tier dpath" of the domain on that device whose
    # path is the longest prefix of the file's path; else the first domain on
    # the device. Prints nothing when no watched domain is on that device.
    local fdev="$1" fpath="$2" domains="$3" k d t tr dp best="" bestlen=-1 first=""
    while read -r k d t tr dp; do
        [[ -n "$k" && "$d" == "$fdev" ]] || continue
        [[ -z "$first" ]] && first="$k $t $tr $dp"
        if [[ "$fpath" == "$dp" || "$fpath" == "$dp"/* || "$dp" == / ]]; then
            if (( ${#dp} > bestlen )); then best="$k $t $tr $dp"; bestlen=${#dp}; fi
        fi
    done <<< "$domains"
    printf '%s\n' "${best:-$first}"
}

_rw_holders() {
    # Holders of one file for the page: "pid comm (parent: comm) cmdline".
    local proc="${DG_PROC:-/proc}" pid comm ppid pcomm cmd
    for pid in "$@"; do
        comm="$(cat "$proc/$pid/comm" 2>/dev/null)" || comm="?"
        ppid="$(awk '/^PPid:/ { print $2; exit }' "$proc/$pid/status" 2>/dev/null)" || ppid=""
        pcomm="?"
        [[ "$ppid" =~ ^[0-9]+$ ]] && pcomm="$(cat "$proc/$ppid/comm" 2>/dev/null)" || true
        # A command line can span many lines (a Claude Code Bash call often does);
        # flatten it before cutting, or every line survives and an oversized
        # body cannot be queued at all (review finding: E2BIG).
        cmd="$(tr '\0\n\r\t' '    ' < "$proc/$pid/cmdline" 2>/dev/null | cut -c1-160)" || cmd=""
        printf '  pid %s %s (parent %s: %s): %s\n' "$pid" "${comm:-?}" "${ppid:-?}" "${pcomm:-?}" "${cmd:-?}"
    done
}

_rw_session_of() {
    # The Claude Code session that owns a path under cc-tmp, or nothing.
    local path="$1" root="${CC_TMP_DIR%/}/"
    [[ "$path" == "$root"claude-* ]] || return 0
    local rel="${path#"$root"}"
    local -a part
    IFS=/ read -r -a part <<< "$rel"
    (( ${#part[@]} >= 3 )) && printf '%s' "${part[2]}"
    return 0
}

# ── The response: pause the command, empty its output ────────
# Only ONE kind of file is ever acted on: a Claude Code background task's
# output, cc-tmp/claude-<uid>/<project>/<session>/tasks/<id>.output. It is the
# tool's own transient record of a command's stdout, and the one file whose
# runaway has filled cc-tmp and broken every session's Bash. Every other file,
# on every filesystem, is page-only: a review measured the database held open
# for writing by about two dozen processes, and pausing one of those stalls
# everything.
#
# The command is PAUSED (SIGSTOP), never killed, and never the session: the
# file is held by the command's own shell and its children, never by the
# `claude` process (MEASURED: fd 1 and 2 belong to the task's bash, parent
# claude). Every holder must descend from a claude process and not be one, or
# nothing is done. The output is opened O_APPEND (MEASURED: fdinfo flags
# 0506001), so after `scripts/watchgod thaw` its writes land at the new end of
# the emptied file, not past a hole.
FROZEN_FILE="$DG_STATE_DIR/frozen"
# The tail kept from an emptied output, for whoever investigates.
DG_RUNAWAY_TAIL_DIR="$HOME/tmp/watchgod-truncated"

_rw_task_output() {
    # 0 when $1 is exactly a Claude Code task output under cc-tmp.
    local path="${1% (deleted)}" root rel
    root="$(cd -P -- "$CC_TMP_DIR" 2>/dev/null && pwd -P)" || return 1
    [[ "$path" == "$root"/* ]] || return 1
    rel="${path#"$root"/}"
    local -a part
    IFS=/ read -r -a part <<< "$rel"
    (( ${#part[@]} == 5 )) || return 1
    [[ "${part[0]}" =~ ^claude-[0-9]+$ && -n "${part[1]}" && -n "${part[2]}" \
       && "${part[3]}" == tasks && "${part[4]}" == ?*.output ]]
}

_rw_claude_depth() {
    # Hops from pid $1 up to its nearest `claude` ancestor; fails when $1 is
    # itself claude or has none within 64 hops (a malformed chain must end).
    local proc="${DG_PROC:-/proc}" pid="$1" hop comm
    comm="$(cat "$proc/$pid/comm" 2>/dev/null)" || return 1
    [[ "$comm" != claude ]] || return 1
    for (( hop = 1; hop <= 64; hop++ )); do
        pid="$(awk '/^PPid:/ { print $2; exit }' "$proc/$pid/status" 2>/dev/null)" || return 1
        [[ "$pid" =~ ^[0-9]+$ ]] && (( pid > 1 )) || return 1
        comm="$(cat "$proc/$pid/comm" 2>/dev/null)" || return 1
        [[ "$comm" == claude ]] && { printf '%s' "$hop"; return 0; }
    done
    return 1
}

_rw_starttime() {
    local rest
    read -r rest 2>/dev/null < "${DG_PROC:-/proc}/$1/stat" || return 1
    rest="${rest##*) }"
    local -a f
    read -r -a f <<< "$rest"
    printf '%s' "${f[19]:-}"
}

_rw_stopped() {
    # 0 once pid $1 reports a stopped state, polling for up to a second.
    local i st
    for (( i = 0; i < 10; i++ )); do
        st="$(awk '/^State:/ { print $2; exit }' "${DG_PROC:-/proc}/$1/status" 2>/dev/null)" || st=""
        [[ "$st" == T || "$st" == t ]] && return 0
        sleep 0.1
    done
    return 1
}

_rw_respond() {
    # Pause every holder of a runaway task output and empty the file.
    # $1 key (dev:inode), $2 path, $3 domain key, $4 size MB, $5 domains,
    # rest: holder pids. Pages what it did this poll; sets _RW_EMPTIED=1
    # when THIS file was emptied, and marks the domain relieved.
    local key="$1" path="$2" dkey="$3" size_mb="$4" domains="$5"
    shift 5
    _RW_EMPTIED=0
    local pid depth st comm ts sorted="" stopped="" newly="" unstopped="" unrecorded="" tail_note emptied=0
    local -A seen=()
    # Parents before children: a child stopped first can be reaped and
    # replaced by its still-running parent before the parent's turn.
    # A holder that has already exited (a loop's short-lived child, gone
    # since the walk) is skipped; one still running without a claude ancestor,
    # or that IS claude, means this is not a session's command: do nothing.
    for pid in "$@"; do
        [[ -n "${seen[$pid]:-}" ]] && continue   # one entry per fd: fd 1 and fd 2
        seen[$pid]=1
        if depth="$(_rw_claude_depth "$pid")"; then
            sorted+="${depth} ${pid}"$'\n'
        elif [[ -e "${DG_PROC:-/proc}/$pid" ]]; then
            _wg_warn_once "rw_veto_${key}" "runaway ${path}: not paused: pid ${pid} holds it and is not a command of a Claude Code session"
            return 0
        fi
    done
    [[ -n "$sorted" ]] || return 0
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    # One deadline for the whole stop loop: a holder in D state never reports
    # stopped, and each would otherwise cost its full second.
    local deadline=$(( SECONDS + 5 ))
    while read -r depth pid; do
        [[ "$pid" =~ ^[0-9]+$ ]] && (( pid > 1 )) || continue
        st="$(_rw_starttime "$pid")" || continue
        comm="$(cat "${DG_PROC:-/proc}/$pid/comm" 2>/dev/null)" || comm="?"
        # Paused on an earlier poll (another holder had kept writing): already
        # recorded, nothing to add.
        if [[ "$(awk '/^State:/ { print $2; exit }' "${DG_PROC:-/proc}/$pid/status" 2>/dev/null)" == T ]]; then
            stopped+=" ${pid}"
            continue
        fi
        if (( SECONDS >= deadline )); then
            unstopped+=" ${pid}"
            continue
        fi
        # Record first so `scripts/watchgod thaw` can find it; on a full $HOME
        # the record can fail, and the pause still happens: an unrecorded
        # pause is named in the page with its manual resume.
        # Under thaw's lock, so a thaw rewriting the file cannot drop this line.
        if ! { mkdir -p "$DG_STATE_DIR" && (
                flock -w 5 9 || exit 1
                printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$pid" "$st" "$comm" "$ts" "$key" "$path" >> "$FROZEN_FILE"
            ) 9>"$FROZEN_FILE.lock"; } 2>/dev/null; then
            unrecorded+=" ${pid}"
        fi
        if kill -STOP "$pid" 2>/dev/null && _rw_stopped "$pid"; then
            stopped+=" ${pid}"
            newly+=" ${pid}"
        else
            unstopped+=" ${pid}"
        fi
    done < <(sort -n <<< "$sorted")
    [[ -n "$stopped" ]] || { log WARN "runaway ${path}: no writer could be paused (${unstopped# })"; return 0; }

    # Reach the file through a PAUSED holder (a stopped process cannot close
    # or reuse a descriptor), open it on a descriptor of our own, and act only
    # if that descriptor is still the file the walk saw. A /proc fd link of a
    # pid that exited and was recycled would name some other file.
    local flink="" ofd="" ffd own=""
    for pid in $stopped; do
        for ffd in "${DG_PROC:-/proc}/$pid"/fd/*; do
            [[ "$(stat -L -c %d:%i -- "$ffd" 2>/dev/null)" == "$key" ]] && { flink="$ffd"; break 2; }
        done
    done
    if [[ -n "$flink" ]] && exec {ofd}>>"$flink" 2>/dev/null; then
        own="/proc/$BASHPID/fd/$ofd"
        [[ "$(stat -L -c %d:%i -- "$own" 2>/dev/null)" == "$key" ]] || own=""
    fi

    # Keep the last MB for whoever investigates, but only where there is room:
    # the copy must never gate the emptying, and never fill a second disk.
    local tdom ttier
    tail_note="no tail kept"
    tdom="$(_rw_domain_for "$(stat -c %d -- "$HOME/tmp" 2>/dev/null)" "$HOME/tmp" "$domains")"
    read -r _ _ ttier _ <<< "$tdom"
    if [[ -n "$own" && ( "${ttier:-}" == green || "${ttier:-}" == yellow ) ]] && mkdir -p "$DG_RUNAWAY_TAIL_DIR" 2>/dev/null; then
        local dest
        dest="$DG_RUNAWAY_TAIL_DIR/${path##*/}.$(date +%s).tail"
        if timeout -k 2 10 tail -c 1048576 -- "$own" > "$dest" 2>/dev/null; then
            tail_note="last 1 MB kept in ${dest}"
        else
            tail_note="the tail could not be kept"
        fi
    fi
    if [[ -n "$own" ]] && truncate -s 0 -- "$own" 2>/dev/null && [[ "$(stat -L -c %s -- "$own" 2>/dev/null)" == 0 ]]; then
        emptied=1
        _RW_EMPTIED=1
        # shellcheck disable=SC2034  # read by handle_fs in tmp_watchgod.sh
        RUNAWAY_RELIEVED[$dkey]=1
    fi
    [[ -n "$ofd" ]] && exec {ofd}>&-

    # Page only when this poll changed something: a writer newly paused, or
    # the file emptied. A truncate that keeps failing under writers paused on
    # an earlier poll is logged, not re-paged every poll.
    local sid body pids="${stopped# }"
    if [[ -z "$newly" ]] && (( ! emptied )); then
        log WARN "runaway ${path}: writers ${pids} stay paused; the file could not be emptied"
        return 0
    fi
    sid="$(_rw_session_of "$path")"
    body="${path} (${size_mb} MB) was filling ${CC_TMP_DIR}."$'\n'
    body+="Paused (SIGSTOP, not killed): pid ${pids}. The Claude Code session itself was NOT paused; a tool call waiting on this command waits until it is resumed or until its own timeout. A paused command keeps any lock it holds."$'\n'
    (( emptied )) && body+="The output file was emptied (${tail_note})."$'\n' \
                  || body+="The output file could NOT be emptied."$'\n'
    [[ -n "$unstopped" ]] && body+="Could not pause: pid${unstopped}."$'\n'
    [[ -n "$sid" ]] && body+="Claude Code session: ${sid}"$'\n'
    body+="Resume: scripts/watchgod thaw all (or thaw <pid>). End it instead: kill -CONT ${pids} && kill ${pids}."
    [[ -n "$unrecorded" ]] && body+=$'\n'"Not recorded (the state disk is full), so thaw cannot see:${unrecorded}; resume with kill -CONT${unrecorded}."
    log WARN "runaway ${path}: paused ${pids}; emptied=${emptied}; ${tail_note}"
    queue_alert_try emergency "watchgod:disk" "Paused a runaway Claude Code task and emptied its output" \
        "$body" "watchgod:runaway-act:${key}:$(date +%s)" \
        || log WARN "could not queue the runaway-response page (alert queue unwritable?)"
    return 0
}


wg_runaway_check() {
    # $1: the watched domains of this poll, one "key dev total_mb path" per
    # line (check_disks builds it). Pages each runaway file once per mode.
    local domains="$1" now key size pid fd dev
    now="$(date +%s)"
    RUNAWAY_FAST=0
    local -A size_of=() dev_of=() pids_of=() fd_of=() seen=()
    local scan scan_rc=""
    scan="$(_rw_scan)" || scan="#rc unknown"
    while read -r key size pid fd dev; do
        [[ -n "$key" ]] || continue
        if [[ "$key" == "#rc" ]]; then scan_rc="$size"; continue; fi
        size_of[$key]="$size"; dev_of[$key]="$dev"
        pids_of[$key]="${pids_of[$key]:-}${pids_of[$key]:+ }$pid"
        fd_of[$key]="${fd_of[$key]:-}${fd_of[$key]:+ }$pid/$fd"
    done <<< "$scan"
    # The pause is vetoed unless the walk completed: the "every holder is a
    # session's command" check is only as complete as the walk that found them.
    local walk_ok=1
    if [[ ! "$scan_rc" =~ ^[01]$ ]]; then
        walk_ok=0
        log WARN "runaway-file walk of /proc did not complete (status ${scan_rc:-missing}; timeout ${DG_RUNAWAY_SCAN_TIMEOUT_S} s) — files it did not report were not checked this poll"
    fi

    local dt=$(( now - _RW_PREV_T )) pg path dom dkey dtotal dtier dpath size_mb raw_b norm_b why sid body link id birth acted
    local -a holders
    pg="$(_wg_mode_tag)"
    for key in "${!size_of[@]}"; do
        # A descriptor may have been closed, its number reused, or its process
        # gone since the walk (a loop's short-lived child): use the first holder
        # whose link still leads to the file measured, and skip the file only
        # if none does.
        local pf
        id=""
        for pf in ${fd_of[$key]}; do
            link="${DG_PROC:-/proc}/${pf%/*}/fd/${pf#*/}"
            read -r id birth < <(stat -L -c '%d:%i %W' -- "$link" 2>/dev/null) || id=""
            [[ "$id" == "$key" ]] && break
        done
        [[ "$id" == "$key" ]] || continue
        path="$(readlink -- "$link" 2>/dev/null)" || path="?"
        [[ "$birth" =~ ^[0-9]+$ ]] || birth=0
        seen[$key]=1
        dom="$(_rw_domain_for "${dev_of[$key]}" "$path" "$domains")"
        [[ -n "$dom" ]] || continue   # not on a watched filesystem
        read -r dkey dtotal dtier dpath <<< "$dom"
        [[ "$dtotal" =~ ^[0-9]+$ ]] && (( dtotal > 0 )) || continue
        size_mb=$(( ${size_of[$key]} / 1048576 ))
        raw_b=-1; norm_b=-1
        if [[ -n "${_RW_PREV[$key]:-}" ]] && (( _RW_PREV_T > 0 && dt > 0 )); then
            raw_b=$(( ${size_of[$key]} - ${_RW_PREV[$key]} ))
            (( raw_b >= 0 )) && norm_b=$(( raw_b * POLL_INTERVAL / dt ))
        fi
        why=""
        if (( size_mb * 100 >= DG_RUNAWAY_PCT * dtotal )); then
            why="it alone is $(( size_mb * 100 / dtotal ))% of ${dpath} (threshold ${DG_RUNAWAY_PCT}%)"
        fi
        if (( norm_b >= 0 && norm_b * 100 >= DG_RUNAWAY_RATE_PCT * dtotal * 1048576 )); then
            why="${why}${why:+; }it grew $(( raw_b / 1048576 )) MB in ${dt} s, $(( norm_b * 100 / (dtotal * 1048576) ))% of ${dpath} per ${POLL_INTERVAL} s (threshold ${DG_RUNAWAY_RATE_PCT}%)"
        fi
        if (( norm_b >= 0 && norm_b * 200 >= DG_RUNAWAY_RATE_PCT * dtotal * 1048576 )); then
            # shellcheck disable=SC2034  # read by check_disks in tmp_watchgod.sh
            RUNAWAY_FAST=1
        fi
        [[ -n "$why" ]] || continue
        read -r -a holders <<< "${pids_of[$key]}"
        # The response, every poll while it holds: only for a Claude Code task
        # output, only once cc-tmp is ORANGE or RED, only when acting.
        acted=0
        if [[ "$dtier" == orange || "$dtier" == red ]] && _rw_task_output "$path"; then
            if (( WATCHGOD_ACT && ! walk_ok )); then
                log WARN "runaway ${path}: not paused this poll: the /proc walk did not complete"
            elif (( WATCHGOD_ACT )); then
                _rw_respond "$key" "$path" "$dkey" "$size_mb" "$domains" "${holders[@]}"
                (( _RW_EMPTIED )) && acted=1
            else
                _wg_warn_once "rw_obs_${key}_${birth}" "OBSERVE: would pause the writers of ${path} (pid ${pids_of[$key]}) and empty it"
            fi
        fi
        [[ " ${_RW_PAGED[$key]:-} " == *" mode${pg} "* ]] && continue
        sid="$(_rw_session_of "$path")"
        body="${path}: ${size_mb} MB, held open for writing. Why flagged: ${why}."$'\n'"Writers:"$'\n'"$(_rw_holders "${holders[@]:0:DG_RUNAWAY_MAX_HOLDERS}")"
        (( ${#holders[@]} > DG_RUNAWAY_MAX_HOLDERS )) && body+=$'\n'"  +$(( ${#holders[@]} - DG_RUNAWAY_MAX_HOLDERS )) more"
        [[ -n "$sid" ]] && body+=$'\n'"Claude Code session: ${sid}"
        if (( acted )); then
            body+=$'\n'"The guardian paused its writers and emptied it; the next page says how to resume."
        else
            body+=$'\n'"The guardian has not acted on it. Stop the writer, or empty the file, before the filesystem fills."
        fi
        if _wg_page critical "Runaway file on ${dpath}: ${path##*/}" "$body" \
                "watchgod:runaway:${key}:${birth}" "CRITICAL — runaway ${path} (${size_mb} MB)"; then
            _RW_PAGED[$key]="${_RW_PAGED[$key]:-} mode${pg}"
            log WARN "runaway file ${path} (${size_mb} MB; ${why}); writers: ${pids_of[$key]}"
        fi
    done

    # Paged-state lives only while a file stays in sight. Back in sight later,
    # it is queued again under the same (file-derived) key, which the drainer
    # collapses; a NEW file has a new birth time, so a new key.
    for key in "${!_RW_PAGED[@]}"; do
        [[ -n "${seen[$key]:-}" ]] || unset "_RW_PAGED[$key]"
    done

    _RW_PREV=()
    for key in "${!size_of[@]}"; do _RW_PREV[$key]="${size_of[$key]}"; done
    _RW_PREV_T=$now
    _rw_repage "$now"
    return 0
}

_rw_repage() {
    # Page again, once per DG_RUNAWAY_REPAGE_H hours, for every recorded pause
    # that is still in place (same pid AND start time) and that old.
    [[ -s "$FROZEN_FILE" ]] || return 0
    local now="$1" pid st comm ts _key path t n=0 list="" pids="" period
    period=$(( now / (DG_RUNAWAY_REPAGE_H * 3600) ))
    (( period != _RW_REPAGED_PERIOD )) || return 0
    while IFS=$'\t' read -r pid st comm ts _key path; do
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        [[ "$(_rw_starttime "$pid" 2>/dev/null || true)" == "$st" ]] || continue
        [[ "$(awk '/^State:/ { print $2; exit }' "${DG_PROC:-/proc}/$pid/status" 2>/dev/null)" == T ]] || continue
        t="$(date -d "$ts" +%s 2>/dev/null)" || continue
        (( now - t >= DG_RUNAWAY_REPAGE_H * 3600 )) || continue
        list+="  pid ${pid} (${comm}) paused since ${ts}, writing ${path}"$'\n'
        pids+=" ${pid}"
        n=$(( n + 1 ))
    done < "$FROZEN_FILE"
    (( n )) || return 0
    if queue_alert_try warning "watchgod:disk" "Still paused: ${n} runaway Claude Code command(s)" \
            "${list}Nothing resumes them on their own. Resume: scripts/watchgod thaw all. End them instead: kill -CONT${pids} && kill${pids}." \
            "watchgod:runaway-paused:${period}"; then
        _RW_REPAGED_PERIOD=$period
    else
        log WARN "could not queue the still-paused page (alert queue unwritable?)"
    fi
    return 0
}
