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
# This file only DETECTS and PAGES. It stops no process and changes no file.
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

# Per file (device:inode): its size at the previous poll, and the modes it has
# been paged in while continuously in sight.
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
    # $1 device number, $2 file path, $3 the domains ("key dev total path"
    # lines). Prints "key total dpath" of the domain on that device whose path
    # is the longest prefix of the file's path; else the first domain on the
    # device. Prints nothing when no watched domain is on that device.
    local fdev="$1" fpath="$2" domains="$3" k d t dp best="" bestlen=-1 first=""
    while read -r k d t dp; do
        [[ -n "$k" && "$d" == "$fdev" ]] || continue
        [[ -z "$first" ]] && first="$k $t $dp"
        if [[ "$fpath" == "$dp" || "$fpath" == "$dp"/* || "$dp" == / ]]; then
            if (( ${#dp} > bestlen )); then best="$k $t $dp"; bestlen=${#dp}; fi
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
        [[ -n "${fd_of[$key]:-}" ]] || fd_of[$key]="$pid/$fd"
    done <<< "$scan"
    if [[ ! "$scan_rc" =~ ^[01]$ ]]; then
        log WARN "runaway-file walk of /proc did not complete (status ${scan_rc:-missing}; timeout ${DG_RUNAWAY_SCAN_TIMEOUT_S} s) — files it did not report were not checked this poll"
    fi

    local dt=$(( now - _RW_PREV_T )) pg path dom dtotal dpath size_mb raw_b norm_b why sid body link id birth
    local -a holders
    pg="$(_wg_mode_tag)"
    for key in "${!size_of[@]}"; do
        link="${DG_PROC:-/proc}/${fd_of[$key]%/*}/fd/${fd_of[$key]#*/}"
        path="$(readlink -- "$link" 2>/dev/null)" || path="?"
        # The descriptor may have been closed and its number reused since the
        # walk: name the file only if the link still leads to the one measured.
        read -r id birth < <(stat -L -c '%d:%i %W' -- "$link" 2>/dev/null) || id=""
        [[ "$id" == "$key" ]] || continue
        [[ "$birth" =~ ^[0-9]+$ ]] || birth=0
        seen[$key]=1
        dom="$(_rw_domain_for "${dev_of[$key]}" "$path" "$domains")"
        [[ -n "$dom" ]] || continue   # not on a watched filesystem
        read -r _ dtotal dpath <<< "$dom"
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
        [[ " ${_RW_PAGED[$key]:-} " == *" mode${pg} "* ]] && continue
        sid="$(_rw_session_of "$path")"
        read -r -a holders <<< "${pids_of[$key]}"
        body="${path}: ${size_mb} MB, held open for writing. Why flagged: ${why}."$'\n'"Writers:"$'\n'"$(_rw_holders "${holders[@]:0:DG_RUNAWAY_MAX_HOLDERS}")"
        (( ${#holders[@]} > DG_RUNAWAY_MAX_HOLDERS )) && body+=$'\n'"  +$(( ${#holders[@]} - DG_RUNAWAY_MAX_HOLDERS )) more"
        [[ -n "$sid" ]] && body+=$'\n'"Claude Code session: ${sid}"
        body+=$'\n'"The guardian has not acted on it. Stop the writer, or empty the file, before the filesystem fills."
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
    return 0
}
