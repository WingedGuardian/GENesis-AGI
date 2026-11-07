# shellcheck shell=bash
# Pause records: every process the disk guardian paused, for `scripts/watchgod`.
#
# One file, $DG_STATE_DIR/frozen, one tab-separated line per paused process:
#   pid  start-time  comm  paused-at  dev:inode  path  state  boot
# A process is its pid AND its start time (stat field 22): every reader matches
# both, so a record never applies to a later holder of the same pid number.
# The start time counts clock ticks since boot, so the pair can recur after a
# reboot: each line also carries the boot it was written in, and a line from
# another boot is not a record (wg_records_list skips it, and the next write
# drops it). Readers see the first seven fields only.
# state is "paused" (the stop is confirmed: State T, or t for a traced process,
# which reports its pending group-stop as t), "pending" (SIGSTOP was sent and
# not yet seen to land), or "pending-t" (sent to a holder already in a tracing
# stop, so only T confirms it).
#
# The rules every reader and writer keeps:
#   * One lock, $DG_STATE_DIR/frozen.lock, around every read-decide-write. The
#     guardian takes it before its first SIGSTOP and keeps it until the record
#     is final; if it cannot get it, it pauses nothing. `scripts/watchgod`
#     waits for it, so it never sees a record before its stop.
#   * Every write replaces the whole file (temp file, then rename), so a
#     reader never sees half a line, and a writer killed mid-way leaves the
#     old file whole.
#   * Files are owner-only (umask 077): a record names a private path.
#   * A path with a tab or newline is refused, and comm has both replaced:
#     either would split or forge a record line.
#
# Pure functions; nothing runs at source time. Needs DG_STATE_DIR.

wg_records_file() { printf '%s/frozen' "${DG_STATE_DIR:?DG_STATE_DIR is unset}"; }

wg_boot_id() {
    # This boot's identity: the kernel boot id. Fails when it cannot be read;
    # nothing is recorded or trusted without it. No fallback: a second source
    # would let one failed read stamp a record as "another boot" and drop the
    # rest (review finding). boot_id exists on every kernel with pidfd.
    local b=""
    read -r b 2>/dev/null < /proc/sys/kernel/random/boot_id || true
    [[ "$b" =~ ^[0-9a-f-]+$ ]] || return 1
    printf '%s' "$b"
}

wg_records_lock() {
    # Take the records lock on a new descriptor, stored in the variable named
    # $1. $2: seconds to wait; empty waits as long as it takes. 0 locked;
    # 1 not locked (nothing is left open).
    # Internal names carry a _wg_ prefix: the nameref would otherwise bind to
    # a local of the same name instead of the caller's variable.
    local -n _wg_lk_out="$1"
    local _wg_wait="${2:-}" _wg_f _wg_fd
    _wg_f="$(wg_records_file)"
    ( umask 077; mkdir -p -- "${_wg_f%/*}" && : >> "$_wg_f.lock" ) 2>/dev/null || return 1
    { exec {_wg_fd}>>"$_wg_f.lock"; } 2>/dev/null || return 1
    if [[ -n "$_wg_wait" ]]; then
        flock -w "$_wg_wait" "$_wg_fd" 2>/dev/null || { exec {_wg_fd}>&-; return 1; }
    else
        flock "$_wg_fd" 2>/dev/null || { exec {_wg_fd}>&-; return 1; }
    fi
    _wg_lk_out=$_wg_fd
}

wg_records_unlock() {
    # Release the lock taken by wg_records_lock (its descriptor is $1).
    local _wg_fd="${1:-}"
    [[ "$_wg_fd" =~ ^[0-9]+$ ]] || return 0
    exec {_wg_fd}>&-
}

wg_records_list() {
    # Every well-formed record of this boot (numeric pid and start time), in
    # order, as its first seven fields. Fails when the file exists but cannot
    # be read, or the boot cannot be identified: an unreadable file is not an
    # empty one, and a writer that took it for one would delete every record.
    local f boot
    f="$(wg_records_file)"
    [[ -e "$f" ]] || return 0
    [[ -r "$f" ]] || return 1
    boot="$(wg_boot_id)" || return 1
    awk -F'\t' -v OFS='\t' -v b="$boot" \
        'NF == 8 && $8 == b && $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ { NF = 7; print }' "$f" 2>/dev/null
}

wg_record_state() {
    # The state of the record for pid $1 with start time $2, or failure when
    # there is none (or the records cannot be read).
    local recs
    recs="$(wg_records_list)" || return 1
    awk -F'\t' -v p="$1" -v s="$2" \
        '$1 == p && $2 == s { print ($7 == "" ? "paused" : $7); f = 1; exit } END { exit !f }' <<< "$recs"
}

_wg_records_replace() {
    # Replace the file with stdin (seven-field lines), atomically, each line
    # stamped with this boot. The caller holds the lock.
    local f boot
    f="$(wg_records_file)"
    boot="$(wg_boot_id)" || return 1
    ( umask 077; awk -v b="$boot" 'NF { print $0 "\t" b }' > "$f.tmp" && mv -f -- "$f.tmp" "$f" ) 2>/dev/null
}

wg_record_put() {
    # Write the record (pid st comm ts key path state), replacing any record
    # for the same pid and start time. A record that stays pending keeps its
    # first time, so re-sending an unconfirmed stop every poll does not reset
    # its reminder clock. The caller holds the lock. Fails on a malformed field.
    # No field may be empty: bash's `IFS=$'\t' read` collapses an empty field
    # where awk keeps it, so the two readers would disagree about the columns.
    local pid="$1" st="$2" comm="$3" ts="$4" key="$5" path="$6" state="$7" recs old line f
    # pid in /proc's own form (no leading zero), or the record never matches.
    # Both in /proc's own form (no leading zero): identities compare as text.
    [[ "$pid" =~ ^[1-9][0-9]*$ && "$st" =~ ^(0|[1-9][0-9]*)$ ]] || return 1
    [[ "$state" == paused || "$state" == pending || "$state" == pending-t ]] || return 1
    for f in "$ts" "$key" "$path"; do
        [[ -n "$f" && "$f" != *$'\t'* && "$f" != *$'\n'* ]] || return 1
    done
    comm="${comm//[$'\t\n']/ }"
    [[ -n "$comm" ]] || comm="?"
    recs="$(wg_records_list)" || return 1
    old="$(awk -F'\t' -v p="$pid" -v s="$st" '$1 == p && $2 == s { print ($7 == "" ? "paused" : $7); exit }' <<< "$recs")"
    if [[ "$old" == pending* && "$state" == pending* ]]; then
        ts="$(awk -F'\t' -v p="$pid" -v s="$st" '$1 == p && $2 == s { print $4; exit }' <<< "$recs")"
        [[ -n "$ts" ]] || ts="$4"
    fi
    line="$(printf '%s\t%s\t%s\t%s\t%s\t%s\t%s' "$pid" "$st" "$comm" "$ts" "$key" "$path" "$state")"
    { [[ -n "$recs" ]] && awk -F'\t' -v p="$pid" -v s="$st" '!($1 == p && $2 == s)' <<< "$recs"
      printf '%s\n' "$line"; } | _wg_records_replace
}

wg_record_promote() {
    # Mark the record of pid $1, start time $2 "paused". The caller holds the
    # lock. Fails, changing nothing, when the records cannot be read.
    local recs
    recs="$(wg_records_list)" || return 1
    [[ -n "$recs" ]] || return 0
    awk -F'\t' -v OFS='\t' -v p="$1" -v s="$2" '$1 == p && $2 == s { $7 = "paused" } { print }' <<< "$recs" \
        | _wg_records_replace
}

wg_record_drop() {
    # Remove the record of pid $1, start time $2. The caller holds the lock.
    # Fails, changing nothing, when the records cannot be read.
    local recs
    recs="$(wg_records_list)" || return 1
    [[ -n "$recs" ]] || return 0
    awk -F'\t' -v p="$1" -v s="$2" '!($1 == p && $2 == s)' <<< "$recs" | _wg_records_replace
}
