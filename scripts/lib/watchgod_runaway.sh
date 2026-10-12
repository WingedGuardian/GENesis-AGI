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
# when it is held open FOR WRITING by a process this uid can see and has at
# least DG_RUNAWAY_MIN_MB ALLOCATED, and fdinfo is read only for those
# candidates. MEASURED on a live install: 30 ms for the filtered walk against
# 297 ms for reading every descriptor's fdinfo.
#
# Size is the space a file actually uses (allocated blocks), never its apparent
# length: a sparse file with a huge length and few blocks fills nothing (review
# finding).
#
# A file is RUNAWAY when either holds:
#   * it uses >= DG_RUNAWAY_PCT % of its domain's total, or
#   * it grows at >= DG_RUNAWAY_RATE_PCT % of that total per POLL_INTERVAL.
# Growth is normalised to POLL_INTERVAL, in bytes, not measured per poll: the
# fast poll is 6x shorter, and a per-poll test would see one sixth of the same
# writer's growth there, drop out of fast mode, and flap (review finding).
# Growth needs two sightings, so a file is never "fast" on its first poll.
# Growth of at least half the rate threshold asks for the fast poll interval.
#
# A file's identity is its GENERATION: device, inode and birth time (stat %W;
# 0 where the filesystem does not record it). Growth history, paging state and
# the page's dedupe key all use it, so a file replaced by another under a reused
# inode starts afresh instead of inheriting the old one's state (review
# finding). It is derived, never remembered: a restart re-sends the same dedupe
# key, which the alert drainer collapses. Known limit: an inode reused within
# the same second, or on a filesystem without birth times, shares a generation.
#
# A file pages once per mode while in sight, and is forgotten only after an
# hour out of sight, so a writer that opens and closes its file cannot re-queue
# the page on every reappearance (review finding: during a delivery outage
# those copies would pile up in the queue).
#
# Pages name processes, never their arguments: a command line can carry a
# credential, and a page leaves the machine (review finding, P1).
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
# A page lists at most this many distinct writers, then "+N more". One line
# each is about 60 characters, so the page stays well inside a chat message.
DG_RUNAWAY_MAX_HOLDERS=20
# Paging state outlives a file's absence by this long.
DG_RUNAWAY_FORGET_S=3600
# A command the guardian paused and nobody resumed is paged again every this
# many hours while it stays paused: nothing resumes it on its own, and a
# dispatched session has no one watching for the first page.
DG_RUNAWAY_REPAGE_H=6
# Per recorded pause ("pid:start:paused-at"): reminders queued so far.
declare -gA _RW_REMINDED=()
# Pids this poll's walk could not inspect (see _rw_blind).
_RW_BLIND=""
# Set by _rw_respond: 1 when it emptied the file it was called for, and 1
# when it paused a writer of it on this poll.
_RW_EMPTIED=0
_RW_PAUSED_NOW=0
# 1 when it sent a SIGSTOP this poll, confirmed or not.
_RW_SENT_NOW=0

# Per generation (device:inode:birth): space used at the previous poll, when it
# was last seen, and the modes it has been paged in.
declare -gA _RW_PREV=() _RW_LAST=() _RW_PAGED=()
# When a baseline in _RW_PREV is older than the last poll: a file an
# incomplete walk did not report keeps its last size and the time it was
# measured, so growth is still judged against it (review finding).
declare -gA _RW_PREV_AT=()
_RW_PREV_T=0
# Set by wg_runaway_check: 1 when some file grew fast enough to want 5 s polls.
# shellcheck disable=SC2034  # read by check_disks in tmp_watchgod.sh
RUNAWAY_FAST=0

_rw_scan() {
    # One line per descriptor held open FOR WRITING on a regular file using at
    # least DG_RUNAWAY_MIN_MB: "<dev:ino> <allocated bytes> <pid> <fd> <dev>".
    # The last line is "#rc <status>": anything above 1 means the walk did not
    # complete, so the caller says so instead of reading silence as "no
    # runaway file". Before it, one "#blind <pid>" line per live process
    # whose descriptors this uid could not inspect (_rw_blind).
    local proc="${DG_PROC:-/proc}" key blocks path pid fd flags rest listing rc=0 min_b
    min_b=$(( DG_RUNAWAY_MIN_MB * 1048576 ))
    # find exits 1 when a descriptor could not be inspected, and that is NOT
    # harmless by itself (review finding): a setuid descendant of a session's
    # command, or a same-uid process that cannot be ptrace-read, may hold the
    # file and stays invisible. MEASURED on a live install: status 1 on every
    # walk (19 processes with unreadable fd directories, 5 same-uid ones whose
    # descriptors deny readlink), so status 1 alone cannot veto, or the
    # response would never run. Instead every cause of it is named as a pid by
    # _rw_blind, and the response refuses when one of those pids is in the
    # session's process tree; a descriptor that vanished mid-walk (the other
    # cause) holds nothing. Anything above 1 (a timeout's 124, or 125-127 and a
    # signal) means the walk did not complete. A killed find also loses output
    # it had found but not yet flushed, so a timed-out walk may miss files it
    # had already reached. -size (apparent length) is only a cheap prefilter
    # and %b (512-byte blocks) decides. A file preallocated past its length
    # (fallocate --keep-size) is missed; a runaway writer appends, so its
    # length leads its allocation.
    listing="$(timeout -k 2 "$DG_RUNAWAY_SCAN_TIMEOUT_S" find -L "$proc"/[0-9]*/fd -mindepth 1 -maxdepth 1 -type f \
                   -size +"$(( DG_RUNAWAY_MIN_MB - 1 ))"M -printf '%D:%i %b %p\n' 2>/dev/null)" || rc=$?
    while read -r key blocks path; do
        [[ -n "$key" && "$blocks" =~ ^[0-9]+$ ]] || continue
        (( blocks * 512 >= min_b )) || continue
        [[ "$path" =~ /([0-9]+)/fd/([0-9]+)$ ]] || continue
        pid="${BASH_REMATCH[1]}"; fd="${BASH_REMATCH[2]}"
        flags=""
        # fdinfo's flags are octal. Read access only (O_RDONLY: the low two
        # bits are 0) is not a writer, whatever the file is doing.
        while read -r rest; do
            [[ "$rest" == flags:* ]] && { flags="${rest#flags:}"; flags="${flags//[[:space:]]/}"; break; }
        done 2>/dev/null < "$proc/$pid/fdinfo/$fd" || true   # stderr first: a vanished pid stays quiet
        if [[ ! "$flags" =~ ^[0-7]+$ ]]; then
            # A live process whose fdinfo cannot be read cannot be judged a
            # non-writer: it is blind, not absent.
            [[ -e "$proc/$pid" ]] && printf '#blind %s\n' "$pid"
            continue
        fi
        (( (8#$flags & 3) != 0 )) || continue
        printf '%s %s %s %s %s\n' "$key" "$(( blocks * 512 ))" "$pid" "$fd" "${key%%:*}"
    done <<< "$listing"
    local brc=0
    _rw_blind "$proc" || brc=$?
    (( brc > 1 && rc <= 1 )) && rc=$brc
    # The status rides the same stdout as the data: a status file would fail to
    # write on a full disk, which is exactly when this must still work.
    printf '#rc %s\n' "$rc"
    return 0
}

_rw_blind() {
    # "#blind <pid>" for every live process under $1 whose descriptors this uid
    # cannot inspect: an fd directory it may not list, or entries whose link
    # it may not read or whose target cannot be stat'ed. Returns the second
    # walk's status (above 1: it did not complete). The walk does not follow
    # links (readlink never touches the target's filesystem); %Y stats the
    # target, so it is bounded like the first.
    local proc="$1" d out rc=0
    for d in "$proc"/[0-9]*; do
        [[ -d "$d/fd" ]] || continue
        [[ -r "$d/fd" && -x "$d/fd" ]] || printf '#blind %s\n' "${d##*/}"
    done
    out="$(timeout -k 2 "$DG_RUNAWAY_SCAN_TIMEOUT_S" find "$proc"/[0-9]*/fd -mindepth 1 -maxdepth 1 \
               -printf '%h\t%Y\t%l\n' 2>/dev/null)" || rc=$?
    awk -F'\t' '$3 == "" || $2 == "?" || $2 == "L" {
        n = split($1, a, "/"); if (a[n] == "fd" && a[n - 1] ~ /^[0-9]+$/) print "#blind " a[n - 1]
    }' <<< "$out" | sort -u
    return "$rc"
}

_rw_command_of() {
    # The pid of the command that pid $1 belongs to: its ancestor (or itself)
    # whose parent is the nearest `claude` process (same walk as
    # _rw_claude_depth), or failure. The blind veto is rooted here, not at the
    # session: an uninspectable sibling of the command (a sandboxed browser
    # the session started, say) holds none of this command's descriptors and
    # must not veto it forever (review finding).
    local proc="${DG_PROC:-/proc}" pid="$1" child hop comm
    for (( hop = 1; hop <= 64; hop++ )); do
        child="$pid"
        pid="$(awk '/^PPid:/ { print $2; exit }' "$proc/$pid/status" 2>/dev/null)" || return 1
        [[ "$pid" =~ ^[0-9]+$ ]] && (( pid > 1 )) || return 1
        comm="$(cat "$proc/$pid/comm" 2>/dev/null)" || return 1
        [[ "$comm" == claude ]] && { printf '%s' "$child"; return 0; }
    done
    return 1
}

_rw_descends() {
    # 0 when pid $1 is $2 or descends from it (64 hops at most).
    local proc="${DG_PROC:-/proc}" pid="$1" hop
    for (( hop = 0; hop <= 64; hop++ )); do
        [[ "$pid" == "$2" ]] && return 0
        pid="$(awk '/^PPid:/ { print $2; exit }' "$proc/$pid/status" 2>/dev/null)" || return 1
        [[ "$pid" =~ ^[0-9]+$ ]] && (( pid > 1 )) || return 1
    done
    return 1
}

_rw_blind_in() {
    # The first pid of the blind list $1 that is in the process tree of a
    # root in $2 (space-separated), or failure.
    local b r st threads
    for b in $1; do
        # A zombie has closed every descriptor, and its fd directory reads as
        # denied: it holds nothing. (MEASURED: the paused command's last
        # `sleep` exits and stays a zombie, unreaped by its stopped parent.)
        # Only a zombie with ONE thread, though: a thread-group leader that
        # called pthread_exit shows Z while its other threads keep running and
        # writing, their descriptors visible only under task/<tid>/fd (review
        # finding, measured: State Z, Threads 2).
        st="$(_rw_state "$b")"
        [[ -z "$st" || "$st" == X ]] && continue
        if [[ "$st" == Z ]]; then
            threads="$(awk '/^Threads:/ { print $2; exit }' "${DG_PROC:-/proc}/$b/status" 2>/dev/null || true)"
            [[ "$threads" == 1 ]] && continue
        fi
        for r in $2; do
            _rw_descends "$b" "$r" && { printf '%s' "$b"; return 0; }
        done
    done
    return 1
}

wg_canon() {
    # $1 with every symlink resolved, or $1 itself when it cannot be resolved.
    # /proc reports a held file by its resolved path, so a watched root must be
    # compared in the same form (review finding: a symlinked cc-tmp matched no
    # root and was judged against the root disk's total).
    local c
    c="$(cd -P -- "$1" 2>/dev/null && pwd -P)" || c=""
    printf '%s' "${c:-$1}"
}

_rw_domain_for() {
    # $1 device number, $2 resolved file path, $3 the domains ("key dev total
    # tier path" lines, paths canonical). Prints "key total tier dpath" of the
    # domain on that device whose root is the longest prefix of the file's
    # path. With no matching root, only a filesystem-wide domain (a mount key,
    # "<dev>m…") may stand in, never a quota domain ("<dev>q…"): a quota bounds
    # only its own tree, and judging an outside file by it pages falsely
    # (review finding). Prints nothing when neither exists.
    local fdev="$1" fpath="$2" domains="$3" k d t tr dp best="" bestlen=-1 fallback=""
    local fk="" ft="" ftr=""
    while read -r k d t tr dp; do
        [[ -n "$k" && "$d" == "$fdev" ]] || continue
        if [[ -z "$fallback" && "$k" == "${d}m"* ]]; then
            fallback="$k $t $tr $dp"; fk="$k"; ft="$t"; ftr="$tr"
        fi
        if [[ "$fpath" == "$dp" || "$fpath" == "$dp"/* || "$dp" == / ]]; then
            if (( ${#dp} > bestlen )); then best="$k $t $tr $dp"; bestlen=${#dp}; fi
        fi
    done <<< "$domains"
    # The fallback's own root is some other watched path on that filesystem;
    # label it with the file's mount point instead, so the page names where
    # the file is (the total, and so the percentage, are the same).
    if [[ -z "$best" && -n "$fallback" ]]; then
        local mnt
        mnt="$(timeout -k 1 2 stat -c %m -- "${fpath%/*}" 2>/dev/null)" || mnt=""
        [[ -n "$mnt" ]] && fallback="$fk $ft $ftr ${mnt}"
    fi
    printf '%s\n' "${best:-$fallback}"
}

_rw_holders() {
    # One line per distinct holder: "pid comm (parent ppid: comm)". Never the
    # command line: arguments can carry credentials, and the page leaves the
    # machine (review finding, P1).
    local proc="${DG_PROC:-/proc}" pid comm ppid pcomm
    for pid in "$@"; do
        comm="$(cat "$proc/$pid/comm" 2>/dev/null)" || comm="?"
        ppid="$(awk '/^PPid:/ { print $2; exit }' "$proc/$pid/status" 2>/dev/null)" || ppid=""
        pcomm="?"
        [[ "$ppid" =~ ^[0-9]+$ ]] && pcomm="$(cat "$proc/$ppid/comm" 2>/dev/null)" || true
        printf '  pid %s %s (parent %s: %s)\n' "$pid" "${comm:-?}" "${ppid:-?}" "${pcomm:-?}"
    done
}

_rw_session_of() {
    # The Claude Code session that owns a resolved path under cc-tmp, or nothing.
    local path="$1" root
    root="$(wg_canon "$CC_TMP_DIR")/"
    [[ "$path" == "$root"claude-* ]] || return 0
    local rel="${path#"$root"}"
    local -a part
    IFS=/ read -r -a part <<< "$rel"
    # claude-<uid>/<project>/<session>/…: a file directly in <project> has none.
    (( ${#part[@]} >= 4 )) && printf '%s' "${part[2]}"
    return 0
}

# ── The response: pause the command, empty its output ────────
# Only ONE kind of file is ever acted on: a Claude Code Bash command's
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

_rw_task_output() {
    # 0 when $1 is exactly a Claude Code task output under cc-tmp.
    local path="${1% (deleted)}" root rel
    # The path goes raw into the tab-separated pause record: a tab or newline
    # in it would split or forge a record line (review finding). No task
    # output Claude Code writes has either.
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
    # Start time of pid $1 (stat field 22); scripts/lib/watchgod_signal.sh.
    wg_starttime "$1"
}

_rw_state() {
    # The one-letter state of pid $1, or nothing when it is gone.
    awk '/^State:/ { print $2; exit }' "${DG_PROC:-/proc}/$1/status" 2>/dev/null || true
}

_rw_stopped() {
    # 0 once pid $1 reports a stopped state while it is still the process that
    # started at $2, polling for up to a second. $3 "T": accept only T (the
    # holder was in a tracing stop before, so t proves nothing).
    local i st
    for (( i = 0; i < 10; i++ )); do
        st="$(_rw_state "$1")"
        if [[ "$st" == T || ( "$st" == t && "${3:-}" != T ) ]]; then
            [[ "$(_rw_starttime "$1" 2>/dev/null || true)" == "$2" ]] && return 0
            return 1
        fi
        sleep 0.1
    done
    return 1
}

# ── Pause records: one lifecycle ────────────────────────────
# FROZEN_FILE holds one line per process the guardian stopped:
#   pid  start-time  comm  paused-at  dev:inode  path  state
# state is "paused" (the stop is confirmed: State T, or t for a traced process,
# which reports its pending group-stop as t), "pending" (SIGSTOP was sent and
# not yet seen to land, e.g. a process in D state), or "pending-t" (sent to a
# holder already in a tracing stop, so only T confirms it). A line without the
# field is "paused". Every reader and writer obeys the same rules (review findings):
#   * A line is written only AFTER its SIGSTOP was sent, under FROZEN_FILE.lock
#     held across send, confirmation and write. `scripts/watchgod thaw` takes
#     the same lock, so it never sees a record before its stop.
#   * "pending" is promoted to "paused" once the stop is seen to land: by the
#     responder, and by the reminder pass (which runs every poll, so a file
#     that stopped qualifying once its writers stopped is still covered).
#   * The responder holds the lock from its first SIGSTOP through the
#     re-check and the truncate, so thaw cannot resume a writer in between.
#   * `watchgod status` calls a record paused only while the same process
#     (start time) is in state T or t.
#   * `watchgod thaw` resumes a "paused" record in state T or t, and a
#     "pending" record in any state (SIGCONT also cancels a stop that has not
#     landed), with a generation-bound SIGCONT, and removes it only once that
#     was sent; it drops, saying so, a "paused" record whose process is gone
#     or running; and it exits nonzero when a record it was asked to clear
#     stays.
#   * Both states are paged when sent and reminded while in place.
#   * Files are created owner-only (umask 077): a record names a private path.
#   * comm and path are written raw into tab-separated fields: a path with a
#     tab or newline is never acted on, and comm has both replaced.

_rw_record_state() {
    # The state of the record for pid $1 with start time $2 ("paused" or
    # "pending"), or failure when there is none.
    [[ -s "$FROZEN_FILE" ]] || return 1
    awk -F'\t' -v p="$1" -v s="$2" '$1 == p && $2 == s { print ($7 == "" ? "paused" : $7); f = 1; exit } END { exit !f }' \
        "$FROZEN_FILE" 2>/dev/null
}

_rw_record_put() {
    # Write the record (pid st comm ts key path state), replacing any line for
    # the same pid and start time. The caller holds FROZEN_FILE.lock.
    local line old ts="$4"
    old="$(_rw_record_state "$1" "$2" || true)"
    if [[ "$old" == pending* && "$7" == pending* ]]; then
        # Still the same unconfirmed stop: keep its time, so re-sending it
        # every poll does not reset its reminder clock.
        ts="$(awk -F'\t' -v p="$1" -v s="$2" '$1 == p && $2 == s { print $4; exit }' "$FROZEN_FILE" 2>/dev/null)" || ts="$4"
        [[ -n "$ts" ]] || ts="$4"
    fi
    line="$(printf '%s\t%s\t%s\t%s\t%s\t%s\t%s' "$1" "$2" "$3" "$ts" "$5" "$6" "$7")"
    if [[ -n "$old" ]]; then
        # ENVIRON, not -v: awk would expand backslashes in a -v value.
        ( umask 077
          RW_LINE="$line" awk -F'\t' -v p="$1" -v s="$2" '$1 == p && $2 == s { print ENVIRON["RW_LINE"]; next } { print }' \
              "$FROZEN_FILE" > "$FROZEN_FILE.tmp" && mv -f -- "$FROZEN_FILE.tmp" "$FROZEN_FILE" ) 2>/dev/null
    else
        ( umask 077; printf '%s\n' "$line" >> "$FROZEN_FILE" ) 2>/dev/null
    fi
}

_rw_record_promote() {
    # Mark the record of pid $1, start time $2 "paused". The caller holds the lock.
    ( umask 077
      awk -F'\t' -v OFS='\t' -v p="$1" -v s="$2" '$1 == p && $2 == s { $7 = "paused" } { print }' \
          "$FROZEN_FILE" > "$FROZEN_FILE.tmp" && mv -f -- "$FROZEN_FILE.tmp" "$FROZEN_FILE" ) 2>/dev/null
}

_rw_unpaused_holders() {
    # After the pause: every process that NOW holds $1 (dev:inode) open for
    # writing and is not stopped, one pid per line. $2: the pids this response
    # confirmed paused itself (space-separated). Stopped means state T; a
    # tracing stop (t) is a debugger's hold that a PTRACE_CONT ends at any
    # moment, so it counts as stopped only for a pid in $2, whose own SIGSTOP
    # stays pending as a group-stop that a traced process reports as t. The walk the response
    # started from is a poll old; since then a holder may have forked a child
    # that inherited the descriptor, or something else may have opened the
    # file. Emptying it under any running writer lets that writer refill it at
    # once (review finding). Prints "#incomplete" when this walk, or the
    # revalidation of what it found, did not complete: then nothing can be
    # confirmed, and the caller must not empty the file.
    local key="$1" ours=" ${2:-} " roots="${3:-}" k s pid fd _d rc="" id st src b
    local -A seen=()
    while read -r k s pid fd _d; do
        if [[ "$k" == "#rc" ]]; then rc="$s"; continue; fi
        if [[ "$k" == "#blind" ]]; then
            # A process in the session's tree that cannot be inspected may be
            # a writer: nothing can be confirmed.
            b="$s"
            _rw_blind_in "$b" "$roots" >/dev/null && { echo "#incomplete"; return 0; }
            continue
        fi
        [[ "$k" == "$key" && -n "$pid" && -z "${seen[$pid]:-}" ]] || continue
        # Still on this file through that descriptor? A gone pid or a closed
        # or reused descriptor is not a holder. stat -L touches the file's
        # filesystem, so it is bounded.
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
        st="$(_rw_state "$pid")"
        [[ -z "$st" || "$st" == T ]] && continue
        [[ "$st" == t && "$ours" == *" ${pid} "* ]] && continue
        printf '%s\n' "$pid"
    done < <(_rw_scan)
    [[ "$rc" =~ ^[01]$ ]] || echo "#incomplete"
    return 0
}

# Action pages not yet in the alert queue, retried every poll (dedupe key ->
# "title<US>body"). After the file is emptied it falls below the detector's
# minimum, so nothing else would ever page that the command was paused (review
# finding). Held in memory: on a full disk there is nowhere durable to hold
# them, and the queue is the durable record once it takes one. A restart drops
# them; the pause records on disk are re-paged by _rw_repage.
declare -gA _RW_PENDING=()

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

_rw_respond() {
    # Pause every holder of a runaway task output and empty the file.
    # $1 key (dev:inode), $2 path, $3 domain key, $4 size MB, $5 domains,
    # rest: holder pids. Pages what it did this poll; sets _RW_EMPTIED=1
    # when THIS file was emptied, and marks the domain relieved.
    local key="$1" path="$2" dkey="$3" size_mb="$4" domains="$5"
    shift 5
    _RW_EMPTIED=0
    _RW_PAUSED_NOW=0
    _RW_SENT_NOW=0
    local pid depth st state comm ts sorted="" stopped="" paused="" newly="" unstopped="" unrecorded=""
    local prestopped="" refused="" unconfirmed="" unconfirmed_new="" running="" tail_note emptied=0 why_not=""
    local -A seen=() st_of=()
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
    # A process in a holder's command tree whose descriptors could not be
    # inspected may hold the file too (a setuid descendant, say): pausing the
    # rest and emptying the file under it is not safe (review finding).
    local roots="" r b
    while read -r _ pid; do
        [[ -n "$pid" ]] || continue
        r="$(_rw_command_of "$pid")" && [[ " ${roots} " != *" ${r} "* ]] && roots+=" ${r}"
    done <<< "$sorted"
    if b="$(_rw_blind_in "${_RW_BLIND:-}" "$roots")"; then
        # Once per file and blind pid: it is re-checked every poll, and a
        # log line per poll would only repeat itself.
        _wg_warn_once "rw_blind_${key}_${b}" "runaway ${path}: not paused: pid ${b}, in the same command's process tree, could not be inspected (its descriptors are unreadable to this uid); re-checked every poll"
        return 0
    fi
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    # One deadline for the whole stop loop: a holder in D state never reports
    # stopped, and each would otherwise cost its full second.
    local deadline=$(( SECONDS + 5 ))
    # The record lifecycle (above): one lock held across every send, its
    # confirmation and its record. Without the lock a pause still happens and
    # is named in the page as unrecorded, with its manual resume.
    local lk="" locked=0 rstate sigrc
    if mkdir -p "$DG_STATE_DIR" 2>/dev/null && ( umask 077; : >> "$FROZEN_FILE.lock" ) 2>/dev/null \
            && { exec {lk}>>"$FROZEN_FILE.lock"; } 2>/dev/null; then
        flock -w 5 "$lk" 2>/dev/null && locked=1
    fi
    while read -r depth pid; do
        [[ "$pid" =~ ^[0-9]+$ ]] && (( pid > 1 )) || continue
        # This holder's generation: every signal below is bound to it.
        st="$(_rw_starttime "$pid")" || continue
        st_of[$pid]="$st"
        comm="$(cat "${DG_PROC:-/proc}/$pid/comm" 2>/dev/null)" || comm="?"
        comm="${comm//[$'\t\n']/ }"   # a field of the tab-separated record
        state="$(_rw_state "$pid")"
        rstate="$(_rw_record_state "$pid" "$st" || true)"
        # Only T is a stop someone made. A tracing stop (t) ends whenever the
        # tracer continues it, so an unrecorded holder in t gets our own
        # SIGSTOP, a record, and confirmation like a running one (review
        # finding). A RECORDED holder in t is ours already: its pending
        # group-stop shows as t.
        if [[ "$state" == T || ( "$state" == t && ( "$rstate" == paused || "$rstate" == pending ) ) ]]; then
            if [[ -n "$rstate" ]]; then
                # Paused on an earlier poll (another holder had kept writing).
                # A pending record whose stop has now landed is promoted.
                if [[ "$rstate" == pending* ]] && ! { (( locked )) && _rw_record_promote "$pid" "$st"; }; then
                    unrecorded+=" ${pid}"
                fi
                paused+=" ${pid}"
            else
                # No record: stopped by something else (job control, a
                # debugger, an operator), or a pause of ours whose record could
                # not be written (the page says which pids those were). Stopped, so it cannot write; but thaw has no record
                # of it, and adopting one would let `thaw all` resume a stop
                # someone made on purpose. The page names it apart, with its
                # own resume (review finding).
                prestopped+=" ${pid}"
            fi
            stopped+=" ${pid}"
            continue
        fi
        if (( SECONDS >= deadline )); then
            unstopped+=" ${pid}"; refused+=" ${pid}"
            continue
        fi
        # Bound to this holder's generation: a pid that exited and was
        # recycled since the start time was read is not signalled (review
        # finding). The record is written only after the send (lifecycle).
        sigrc=0
        wg_signal_gen STOP "$pid" "$st" || sigrc=$?
        (( sigrc == 1 )) && continue   # exited since the walk: not a holder any more
        if (( sigrc != 0 )); then
            unstopped+=" ${pid}"; refused+=" ${pid}"   # nothing was sent, nothing recorded
            continue
        fi
        # A holder already in a tracing stop (t) before our SIGSTOP reads t
        # whether or not our stop landed: only T confirms it (review finding).
        if _rw_stopped "$pid" "$st" "$([[ "$state" == t || "$rstate" == pending-t ]] && echo T)"; then
            rstate=paused
            stopped+=" ${pid}"
            paused+=" ${pid}"
            newly+=" ${pid}"
        else
            # Sent, not seen to land (D state, or a traced holder). Recorded
            # pending: paged, reminded, promoted once it lands, and resumed by
            # thaw, whose SIGCONT also cancels a stop still pending.
            # pending-t: the holder was in t, so only T will confirm it.
            if [[ "$state" == t || "$rstate" == pending-t ]]; then rstate=pending-t; else rstate=pending; fi
            unstopped+=" ${pid}"; unconfirmed+=" ${pid}"
            # Paged once, when first sent: a stop still pending on later polls
            # is news only to the reminder.
            [[ "$(_rw_record_state "$pid" "$st" || true)" == pending* ]] || unconfirmed_new+=" ${pid}"
        fi
        if ! { (( locked )) && _rw_record_put "$pid" "$st" "$comm" "$ts" "$key" "$path" "$rstate"; }; then
            unrecorded+=" ${pid}"
        fi
    done < <(sort -n <<< "$sorted")
    if [[ -z "$stopped" && -z "$unconfirmed" ]]; then
        [[ -n "$lk" ]] && exec {lk}>&-
        log WARN "runaway ${path}: no writer could be paused (${unstopped# })"
        return 0
    fi
    [[ -n "$stopped" ]] || why_not="no writer was confirmed paused"

    # Empty the file only when NO live writer is left running (review finding:
    # one paused holder out of several used to be enough). Both lists count:
    # the holders this response could not pause, and whatever holds the file
    # for writing NOW (a child forked since the walk inherits the descriptor).
    # Those it did pause stay paused, and the next poll tries the rest again.
    local p
    for p in $unstopped; do
        [[ "$(_rw_starttime "$p" 2>/dev/null || true)" == "${st_of[$p]}" ]] || continue   # exited since
        running+=" ${p}"
    done
    while read -r p; do
        [[ -n "$p" ]] || continue
        if [[ "$p" == "#incomplete" ]]; then
            why_not="the check for running writers did not complete"
            continue
        fi
        [[ " ${running} " == *" ${p} "* ]] || running+=" ${p}"
    done < <(_rw_unpaused_holders "$key" "$stopped" "$roots")
    [[ -n "$running" ]] && why_not="${why_not}${why_not:+; }still running: pid${running}"

    # Reach the file through a PAUSED holder (a stopped process cannot close
    # or reuse a descriptor), open it on a descriptor of our own, and act only
    # if that descriptor is still the file the walk saw. A /proc fd link of a
    # pid that exited and was recycled would name some other file.
    local flink="" ofd="" ffd own=""
    if [[ -z "$why_not" ]]; then
        for pid in $stopped; do
            for ffd in "${DG_PROC:-/proc}/$pid"/fd/*; do
                # readlink never touches the target's filesystem; only a link that
                # names this file is stat'ed, and under a timeout (a holder's other
                # descriptors may sit on a stalled mount).
                [[ "$(readlink -- "$ffd" 2>/dev/null)" == "$path" ]] || continue
                [[ "$(timeout -k 1 2 stat -L -c %d:%i -- "$ffd" 2>/dev/null)" == "$key" ]] && { flink="$ffd"; break 2; }
            done
        done
        if [[ -n "$flink" ]] && { exec {ofd}>>"$flink"; } 2>/dev/null; then
            own="/proc/$BASHPID/fd/$ofd"
            [[ "$(stat -L -c %d:%i -- "$own" 2>/dev/null)" == "$key" ]] || own=""
        fi
    fi

    # Keep the last MB for whoever investigates, but only where there is room:
    # the copy must never gate the emptying, and never fill a second disk.
    local tdom ttier
    tail_note="no tail kept"
    tdom="$(_rw_domain_for "$(stat -L -c %d -- "$HOME/tmp" 2>/dev/null)" "$(wg_canon "$HOME/tmp")" "$domains")"
    read -r _ _ ttier _ <<< "$tdom"
    # The copy is the private stdout of a command, which can carry a
    # credential: owner-only, in an owner-only directory, under a name no
    # other tail can take (review findings). mktemp creates the file 0600 and
    # exclusively, so an existing tail is never overwritten.
    if [[ -n "$own" && ( "${ttier:-}" == green || "${ttier:-}" == yellow ) ]]; then
        local dest=""
        if _rw_private_dir "$DG_RUNAWAY_TAIL_DIR"; then
            dest="$(umask 077; mktemp --suffix=.tail "$DG_RUNAWAY_TAIL_DIR/${path##*/}.$(date +%s).XXXXXXXX" 2>/dev/null)" || dest=""
        fi
        if [[ -n "$dest" ]] && timeout -k 2 10 tail -c 1048576 -- "$own" > "$dest" 2>/dev/null; then
            tail_note="last 1 MB kept in ${dest}"
        else
            [[ -n "$dest" ]] && rm -f -- "$dest" 2>/dev/null
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
    # Released only now: holding the record lock from the first SIGSTOP
    # through the bounded re-check, tail copy and truncate keeps `thaw` from
    # resuming a writer between the re-check and the truncate. (Were it
    # released earlier, a writer thawed in that gap would only lose what it
    # wrote before the truncate: the output is O_APPEND, so no hole.)
    [[ -n "$lk" ]] && exec {lk}>&-

    # Page only when this poll changed something: a writer newly paused, or
    # the file emptied. A truncate that keeps failing under writers paused on
    # an earlier poll is logged, not re-paged every poll.
    local sid body title pids="${paused# }" all="${stopped# }${unconfirmed}"
    all="${all# }"
    # A stop sent this poll is news even when it is not confirmed (review
    # finding: a pending-only poll paged nothing).
    if [[ -z "$newly" && -z "$unconfirmed_new" ]] && (( ! emptied )); then
        log WARN "runaway ${path}: writers ${all} stay paused; the file could not be emptied${why_not:+ (${why_not})}"
        return 0
    fi
    sid="$(_rw_session_of "$path")"
    body="${path} (${size_mb} MB) was filling ${CC_TMP_DIR}."$'\n'
    [[ -n "$pids" ]] && body+="Paused (SIGSTOP, not killed): pid ${pids}. "
    body+="The Claude Code session itself was NOT paused; a tool call waiting on this command waits until it is resumed or until its own timeout. A paused command keeps any lock it holds."$'\n'
    [[ -n "$prestopped" ]] && body+="Stopped, with no pause record (stopped by something else, or an earlier pause of the guardian's that could not be recorded; thaw does not resume these): pid${prestopped}; resume with kill -CONT${prestopped}."$'\n'
    if (( emptied )); then
        body+="The output file was emptied (${tail_note})."$'\n'
    else
        body+="The output file could NOT be emptied${why_not:+ (${why_not})}."$'\n'
    fi
    [[ -n "$refused" ]] && body+="Could not pause: pid${refused}."$'\n'
    [[ -n "$unconfirmed" ]] && body+="SIGSTOP sent but NOT yet confirmed (the process has not stopped yet): pid${unconfirmed}. Recorded as pending; scripts/watchgod thaw resumes it, and its SIGCONT also cancels a stop that has not landed."$'\n'
    [[ -n "$sid" ]] && body+="Claude Code session: ${sid}"$'\n'
    [[ -n "$pids$unconfirmed" ]] && body+="Resume: scripts/watchgod thaw all (or thaw <pid>). "
    body+="End it instead: kill -CONT ${all} && kill ${all}."
    [[ -n "$unrecorded" ]] && body+=$'\n'"Not recorded as paused (the record could not be written: a full state disk, or its lock was held), so thaw cannot resume:${unrecorded}; resume with kill -CONT${unrecorded}."
    [[ -n "$newly" ]] && _RW_PAUSED_NOW=1
    [[ -n "$newly$unconfirmed_new" ]] && _RW_SENT_NOW=1
    if (( emptied )) && [[ -z "$newly" ]]; then
        # No signal was sent this poll: every writer was already stopped.
        title="Emptied a runaway Claude Code task output (its writers were already stopped)"
    elif (( emptied )); then
        title="Paused a runaway Claude Code task and emptied its output"
    elif [[ -n "$newly" ]]; then
        # The title is what an alert listing shows: it must not claim the
        # recovery in the case that still needs someone (review finding).
        title="Paused a runaway Claude Code task; its output could NOT be emptied"
    else
        title="Sent SIGSTOP to a runaway Claude Code task, NOT yet confirmed; its output could NOT be emptied"
    fi
    log WARN "runaway ${path}: paused ${pids:-none}; already stopped${prestopped:- none}; emptied=${emptied}; ${tail_note}${why_not:+; ${why_not}}"
    _rw_page_act "watchgod:runaway-act:${key}:$(date +%s)" "$title" "$body"
    return 0
}


wg_runaway_check() {
    # $1: the watched domains of this poll, one "key dev total_mb tier path" per
    # line, paths canonical (check_disks builds it). Pages each runaway file
    # once per mode.
    local domains="$1" now key size pid fd dev
    now="$(date +%s)"
    RUNAWAY_FAST=0
    # An action page an earlier poll could not queue goes first.
    _rw_flush_pending
    local -A size_of=() dev_of=() fds_of=()
    local scan scan_rc=""
    _RW_BLIND=""
    scan="$(_rw_scan)" || scan="#rc unknown"
    while read -r key size pid fd dev; do
        [[ -n "$key" ]] || continue
        if [[ "$key" == "#rc" ]]; then scan_rc="$size"; continue; fi
        if [[ "$key" == "#blind" ]]; then _RW_BLIND+=" ${size}"; continue; fi
        size_of[$key]="$size"; dev_of[$key]="$dev"
        fds_of[$key]="${fds_of[$key]:-}${fds_of[$key]:+ }$pid/$fd"
    done <<< "$scan"
    # The pause is vetoed unless the walk completed: the "every holder is a
    # session's command" check is only as complete as the walk that found them.
    local walk_ok=1
    if [[ ! "$scan_rc" =~ ^[01]$ ]]; then
        walk_ok=0
        log WARN "runaway-file walk of /proc did not complete (status ${scan_rc:-missing}; timeout ${DG_RUNAWAY_SCAN_TIMEOUT_S} s) — files it did not report were not checked this poll"
    fi

    local dt pg path dom dkey dtotal dtier dpath size_mb raw_b norm_b why sid body acted responded
    local link id birth gen pf pfpid st_line st_rc=0
    local -A size_now=() st_of=()
    local -a holders links=()
    pg="$(_wg_mode_tag)"
    # Every descriptor is re-checked in ONE bounded stat: a held file on a
    # network or FUSE mount that stalled after the walk must not hang the poll
    # loop (review finding). /proc readlink never touches the target's
    # filesystem; stat -L does. stat exits 1 when some link failed: a link
    # that is gone is no holder, and one still there is named blind below.
    for key in "${!fds_of[@]}"; do
        for pf in ${fds_of[$key]}; do links+=("${DG_PROC:-/proc}/${pf%/*}/fd/${pf#*/}"); done
    done
    if (( ${#links[@]} )); then
        while IFS=$'\t' read -r link st_line; do
            [[ -n "$link" ]] && st_of[$link]="$st_line"
        done < <(rc=0
                 timeout -k 2 "$DG_RUNAWAY_SCAN_TIMEOUT_S" stat -L -c $'%n\t%d:%i %W' -- "${links[@]}" 2>/dev/null || rc=$?
                 printf '#rc\t%s\n' "$rc")
        st_rc="${st_of["#rc"]:-unknown}"
        if [[ ! "$st_rc" =~ ^[01]$ ]]; then
            walk_ok=0
            log WARN "runaway-file revalidation did not complete (status ${st_rc}; timeout ${DG_RUNAWAY_SCAN_TIMEOUT_S} s) — files were not revalidated this poll (a killed stat loses its buffered output)"
        fi
    fi
    for key in "${!size_of[@]}"; do
        # Every descriptor is re-checked: since the walk, one may have been
        # closed, its number reused, or its process gone. A holder counts only
        # through a descriptor that still leads to the file measured, each
        # holder once (review findings). No such descriptor: skip the file.
        link=""; birth=""; holders=()
        local -A counted=()
        for pf in ${fds_of[$key]}; do
            pfpid="${pf%/*}"
            local l="${DG_PROC:-/proc}/${pfpid}/fd/${pf#*/}" b=""
            read -r id b <<< "${st_of[$l]:-}" || id=""
            # No result for a descriptor that is still there: stat failed on
            # it, so its process is a holder that could not be checked.
            [[ -z "$id" && -L "$l" ]] && _RW_BLIND+=" ${pfpid}"
            [[ "$id" == "$key" ]] || continue
            [[ -n "$link" ]] || { link="$l"; birth="$b"; }
            [[ -n "${counted[$pfpid]:-}" ]] && continue
            counted[$pfpid]=1
            holders+=("$pfpid")
        done
        unset counted
        [[ -n "$link" ]] || continue
        [[ "$birth" =~ ^[0-9]+$ ]] || birth=0
        gen="${key}:${birth}"
        _RW_LAST[$gen]=$now
        size_now[$gen]="${size_of[$key]}"
        path="$(readlink -- "$link" 2>/dev/null)" || path="?"
        dom="$(_rw_domain_for "${dev_of[$key]}" "${path% (deleted)}" "$domains")"
        [[ -n "$dom" ]] || continue   # not on a watched filesystem
        read -r dkey dtotal dtier dpath <<< "$dom"
        [[ "$dtotal" =~ ^[0-9]+$ ]] && (( dtotal > 0 )) || continue
        size_mb=$(( ${size_of[$key]} / 1048576 ))
        raw_b=-1; norm_b=-1
        dt=$(( now - ${_RW_PREV_AT[$gen]:-$_RW_PREV_T} ))
        if [[ -n "${_RW_PREV[$gen]:-}" ]] && (( _RW_PREV_T > 0 && dt > 0 )); then
            raw_b=$(( ${size_of[$key]} - ${_RW_PREV[$gen]} ))
            (( raw_b >= 0 )) && norm_b=$(( raw_b * POLL_INTERVAL / dt ))
        fi
        why=""
        if (( size_mb * 100 >= DG_RUNAWAY_PCT * dtotal )); then
            why="it alone uses $(( size_mb * 100 / dtotal ))% of ${dpath} (threshold ${DG_RUNAWAY_PCT}%)"
        fi
        if (( norm_b >= 0 && norm_b * 100 >= DG_RUNAWAY_RATE_PCT * dtotal * 1048576 )); then
            why="${why}${why:+; }it grew $(( raw_b / 1048576 )) MB in ${dt} s, $(( norm_b * 100 / (dtotal * 1048576) ))% of ${dpath} per ${POLL_INTERVAL} s (threshold ${DG_RUNAWAY_RATE_PCT}%)"
        fi
        if (( norm_b >= 0 && norm_b * 200 >= DG_RUNAWAY_RATE_PCT * dtotal * 1048576 )); then
            # shellcheck disable=SC2034  # read by check_disks in tmp_watchgod.sh
            RUNAWAY_FAST=1
        fi
        [[ -n "$why" ]] || continue
        # The response, every poll while it holds: only for a Claude Code task
        # output, only once cc-tmp is ORANGE or RED, only when acting.
        acted=0; responded=0
        if [[ "$dtier" == orange || "$dtier" == red ]] && _rw_task_output "$path"; then
            if (( WATCHGOD_ACT && ! walk_ok )); then
                log WARN "runaway ${path}: not paused this poll: the /proc walk did not complete"
            elif (( WATCHGOD_ACT )); then
                _rw_respond "$key" "$path" "$dkey" "$size_mb" "$domains" "${holders[@]}"
                responded=1
                (( _RW_EMPTIED )) && acted=1
            else
                _wg_warn_once "rw_obs_${gen}" "OBSERVE: would pause the writers of ${path} (pid ${holders[*]}) and empty it"
            fi
        fi
        [[ " ${_RW_PAGED[$gen]:-} " == *" mode${pg} "* ]] && continue
        sid="$(_rw_session_of "${path% (deleted)}")"
        body="${path}: ${size_mb} MB in use, held open for writing. Why flagged: ${why}."$'\n'"Writers:"$'\n'"$(_rw_holders "${holders[@]:0:DG_RUNAWAY_MAX_HOLDERS}")"
        (( ${#holders[@]} > DG_RUNAWAY_MAX_HOLDERS )) && body+=$'\n'"  +$(( ${#holders[@]} - DG_RUNAWAY_MAX_HOLDERS )) more"
        [[ -n "$sid" ]] && body+=$'\n'"Claude Code session: ${sid}"
        if (( acted && _RW_PAUSED_NOW )); then
            body+=$'\n'"The guardian paused its writers and emptied it; its companion action page says how to resume."
        elif (( acted )); then
            body+=$'\n'"The guardian emptied it; its writers were already stopped (its companion action page says by whom and how to resume)."
        elif (( responded && _RW_SENT_NOW )); then
            body+=$'\n'"The guardian sent its writers SIGSTOP but has NOT emptied it; its companion action page says what is still running and how to resume."
        else
            body+=$'\n'"The guardian has not acted on it. Stop the writer, or empty the file, before the filesystem fills."
        fi
        if _wg_page critical "Runaway file on ${dpath}: ${path##*/}" "$body" \
                "watchgod:runaway:${gen}" "CRITICAL — runaway ${path} (${size_mb} MB)"; then
            _RW_PAGED[$gen]="${_RW_PAGED[$gen]:-} mode${pg}"
            log WARN "runaway file ${path} (${size_mb} MB; ${why}); writers: ${holders[*]}"
        fi
    done

    # Out of sight for DG_RUNAWAY_FORGET_S: forgotten. Seen again later, it is
    # queued again under the same key, which the drainer collapses. Only a
    # complete walk proves a file absent: during an outage of the walk nothing
    # is forgotten, or the baselines it carries would expire (review finding).
    if (( walk_ok )); then
        for gen in "${!_RW_LAST[@]}"; do
            (( now - ${_RW_LAST[$gen]} > DG_RUNAWAY_FORGET_S )) || continue
            unset "_RW_LAST[$gen]" "_RW_PAGED[$gen]" "_RW_PREV[$gen]" "_RW_PREV_AT[$gen]"
        done
    fi

    # A file an incomplete walk did not report keeps its baseline and its time:
    # dropping it would make the next poll a first sighting, and repeated
    # incomplete walks would hide a fast grower for good (review finding).
    local -A carry=() carry_at=()
    if (( ! walk_ok )); then
        for gen in "${!_RW_PREV[@]}"; do
            [[ -n "${size_now[$gen]:-}" ]] && continue
            carry[$gen]="${_RW_PREV[$gen]}"
            carry_at[$gen]="${_RW_PREV_AT[$gen]:-$_RW_PREV_T}"
        done
    fi
    _RW_PREV=(); _RW_PREV_AT=()
    for gen in "${!size_now[@]}"; do _RW_PREV[$gen]="${size_now[$gen]}"; done
    for gen in "${!carry[@]}"; do
        _RW_PREV[$gen]="${carry[$gen]}"; _RW_PREV_AT[$gen]="${carry_at[$gen]}"
    done
    _RW_PREV_T=$now
    _rw_repage "$now"
    return 0
}

_rw_repage() {
    # Page again for every recorded pause still in place (same pid AND start
    # time, state T or t), DG_RUNAWAY_REPAGE_H hours after ITS OWN pause and
    # every DG_RUNAWAY_REPAGE_H hours after that (review finding: a global
    # epoch bucket let one command's reminder delay another's by up to a whole
    # period). _RW_REMINDED counts, per pause, the reminders already queued; a
    # restart forgets it, so each pause older than a period gets one reminder
    # on the first poll, then resumes its own schedule.
    [[ -s "$FROZEN_FILE" ]] || return 0
    local now="$1" pid st comm ts _key path rstate t k g n=0 list="" pids="" due="" live note lk=""
    local period=$(( DG_RUNAWAY_REPAGE_H * 3600 ))
    local -A present=()
    # Promote every "pending" record whose stop has landed (same process, T or
    # t), under the record lock (review finding: once its writers stopped, the
    # file no longer qualified and the responder never ran again to do it).
    if grep -qE $'\tpending(-t)?$' "$FROZEN_FILE" 2>/dev/null && { exec {lk}>>"$FROZEN_FILE.lock"; } 2>/dev/null; then
        if flock -w 5 "$lk" 2>/dev/null; then
            while IFS=$'\t' read -r pid st comm ts _key path rstate; do
                [[ "$pid" =~ ^[0-9]+$ && "$rstate" == pending* ]] || continue
                [[ "$(_rw_starttime "$pid" 2>/dev/null || true)" == "$st" ]] || continue
                live="$(_rw_state "$pid")"
                [[ "$live" == T || ( "$live" == t && "$rstate" == pending ) ]] || continue
                _rw_record_promote "$pid" "$st" && log INFO "pause of pid ${pid} (${comm}) confirmed after the fact"
            done < "$FROZEN_FILE"
        fi
        exec {lk}>&-
    fi
    while IFS=$'\t' read -r pid st comm ts _key path rstate; do
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        [[ "$(_rw_starttime "$pid" 2>/dev/null || true)" == "$st" ]] || continue
        live="$(_rw_state "$pid")"
        note=""
        if [[ "${rstate:-paused}" == pending* ]]; then
            # Reminded whatever its state: a stop that never lands still needs
            # someone (review finding).
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
    done < "$FROZEN_FILE"
    for g in "${!_RW_REMINDED[@]}"; do
        [[ -n "${present[$g]:-}" ]] || unset "_RW_REMINDED[$g]"
    done
    (( n )) || return 0
    if queue_alert_try warning "watchgod:disk" "Still paused: ${n} runaway Claude Code command(s)" \
            "${list}Nothing resumes them on their own. Resume: scripts/watchgod thaw all. End them instead: kill -CONT${pids} && kill${pids}." \
            "watchgod:runaway-paused:$(printf '%s' "$due" | cksum | cut -d' ' -f1)"; then
        for g in $due; do _RW_REMINDED[${g%=*}]="${g##*=}"; done
    else
        log WARN "could not queue the still-paused page (alert queue unwritable?); retrying every poll"
    fi
    return 0
}
