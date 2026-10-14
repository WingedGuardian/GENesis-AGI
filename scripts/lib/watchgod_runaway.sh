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
# a Claude Code Bash command's tasks/*.output in cc-tmp that is GROWING fast
# (the growth test, live this poll), while its domain is ORANGE or RED and the
# guardian is in act mode. Then it pauses that command (SIGSTOP: never the
# session, never a kill) and records the pause for `scripts/watchgod thaw|kill`.
# It does not empty the file (a later change adds that). Nothing else is
# stopped or changed.
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
# the same second, or on a filesystem without birth times, shares a generation,
# and with it a paged file's state for up to DG_RUNAWAY_PAGED_FORGET_S.
#
# A file pages once per mode per EPISODE. An episode ends when a paged file is
# seen at half its size at the previous sighting or less (emptied, truncated,
# rotated by copy-truncate): the paged modes are cleared, and a refill pages
# again under its own key, "<generation>:e<start time>" (#3023). Never on the
# poll that ended it: a shrink is the cure, not a new runaway. Half, not "any
# decrease": allocated size moves a little on its own.
# A paged file is FOLLOWED: when the scan does not report it (it reports only
# files over the floor), its writers' descriptors from the last sighting are
# re-checked, and one that still leads to the same file, still open for
# writing, gives its size, however small. So a held file emptied is seen
# empty, never inferred from a later size (review finding: a refill within one
# poll was missed). A followed file under the floor updates the baseline and
# is never paged itself. Only when no writer was seen at all (the old writer
# gone, a new one truncating on open) is the size inferred on return: at least
# halfway down from its last size to the floor (review findings: a reopen a
# little smaller is not an empty file).
# What counts as a refill: growth fast enough for the growth threshold, or a
# CROSSING of the size threshold from below. A file still over the size
# threshold after its shrink is not paged again for its size until it has been
# seen under it (review finding: the size alone would re-page a file that was
# shrunk and left). Known limit, accepted: a file cut to a size still over the
# threshold that then regrows slowly is not paged again; one size page covers
# one stretch over the threshold.
# The first episode keeps the bare generation key, so a restart still collapses
# with the page it re-sends. Episodes live in memory: after a restart, a refill
# pages under the bare key, which the drainer suppresses while it still holds
# the first page (24 h) (accepted).
# A page that could not be queued stays DUE: its reason is kept and sent on the
# next poll even if the condition that raised it has passed (review finding: a
# crossing seen once, on a poll whose enqueue failed, was otherwise lost).
#
# A file out of sight keeps its last size and when it was measured, so a writer
# that closes its file for a poll and reopens it is judged against that
# baseline over the whole gap, not as a first sighting (#3024). An unpaged file
# is forgotten after DG_RUNAWAY_FORGET_S out of sight; a paged one only after
# DG_RUNAWAY_PAGED_FORGET_S, the alert drainer's dedupe window, so a writer that
# opens and closes its file cannot re-queue the page on every reappearance
# (review finding: during a delivery outage those copies would pile up), and a
# slow refill still finds the baseline that ends its episode.
#
# Pages name processes, never their arguments: a command line can carry a
# credential, and a page leaves the machine (review finding, P1).
#
# A holder's identity is read through ONE handle on its /proc entry
# (scripts/lib/wg_proc.py): the descriptor still leading to the file, the
# process's start time, its name and its parent all come from that process,
# never from a pid that was reused in between. _RW_IDENT[pid] holds what it
# read this poll ("starttime state comm ppid pcomm", tab-separated, "?" where
# it could not tell). Names are made safe for a page there: a process chooses
# its own name, and a newline in it could forge page lines. Without python3,
# or if the read does not finish, the holders are the ones the stat above
# found, with unknown identity and control bytes removed from their names.
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
# An unpaged file's state outlives its absence by this long.
DG_RUNAWAY_FORGET_S=3600
# A paged file's state outlives its absence by this long: the alert drainer's
# dedupe window (governance's 24 h default for queued alerts). Forgotten
# sooner, a refill would re-send the bare key while the drainer still holds it.
DG_RUNAWAY_PAGED_FORGET_S=86400

# Per generation (device:inode:birth): space used at the previous poll, when it
# was last seen, and the modes it has been paged in.
declare -gA _RW_PREV=() _RW_LAST=() _RW_PAGED=()
# Per generation: when its current episode began, for every episode after the
# first (unset for the first one).
declare -gA _RW_EP=()
# Per generation: the writers' descriptors ("pid/fd ...") it was last seen
# through, to follow it once paged. Per generation AND mode ("<gen><mode tag>"):
# the reason of a page that could not be queued, sent on a later poll in that
# mode only, and cleared only by that mode's page.
declare -gA _RW_LINKS=() _RW_DUE=()
# When a baseline in _RW_PREV is older than the last poll: a file an
# incomplete walk did not report keeps its last size and the time it was
# measured, so growth is still judged against it (review finding).
declare -gA _RW_PREV_AT=()
_RW_PREV_T=0
# Per pid, this poll only: the identity wg_proc.py read (see the header).
declare -gA _RW_IDENT=()
_RW_PROC_PY="${BASH_SOURCE[0]%/*}/wg_proc.py"
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
# Set by wg_runaway_check: 1 when some file grew fast enough to want 5 s polls.
# shellcheck disable=SC2034  # read by check_disks in tmp_watchgod.sh
RUNAWAY_FAST=0

_rw_writer() {
    # 0 when descriptor $2 of pid $1 is open for writing. fdinfo's flags are
    # octal; read access only (O_RDONLY: the low two bits are 0) is not a
    # writer, whatever the file is doing.
    local rest flags=""
    while read -r rest; do
        [[ "$rest" == flags:* ]] && { flags="${rest#flags:}"; flags="${flags//[[:space:]]/}"; break; }
    done 2>/dev/null < "${DG_PROC:-/proc}/$1/fdinfo/$2" || true   # stderr first: a vanished pid stays quiet
    [[ "$flags" =~ ^[0-7]+$ ]] && (( (8#$flags & 3) != 0 ))
}

_rw_scan() {
    # One line per descriptor held open FOR WRITING on a regular file using at
    # least DG_RUNAWAY_MIN_MB: "<dev:ino> <allocated bytes> <pid> <fd> <dev>".
    # The last line is "#rc <status>": anything above 1 means the walk did not
    # complete, so the caller says so instead of reading silence as "no
    # runaway file".
    local proc="${DG_PROC:-/proc}" key blocks path pid fd listing rc=0 min_b
    min_b=$(( DG_RUNAWAY_MIN_MB * 1048576 ))
    # find exits 1 on the unreadable descriptors of other users' processes,
    # which is normal. Anything above 1 (a timeout's 124, or 125-127 and a
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
        _rw_writer "$pid" "$fd" || continue
        printf '%s %s %s %s %s\n' "$key" "$(( blocks * 512 ))" "$pid" "$fd" "${key%%:*}"
    done <<< "$listing"
    # The status rides the same stdout as the data: a status file would fail to
    # write on a full disk, which is exactly when this must still work.
    printf '#rc %s\n' "$rc"
    return 0
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
    # path" lines, paths canonical). Prints "key total dpath" of the domain on
    # that device whose root is the longest prefix of the file's path. With no
    # matching root, only a filesystem-wide domain (a mount key, "<dev>m…")
    # may stand in, never a quota domain ("<dev>q…"): a quota bounds only its
    # own tree, and judging an outside file by it pages falsely (review
    # finding). Prints nothing when neither exists.
    local fdev="$1" fpath="$2" domains="$3" k d t dp best="" bestlen=-1 fallback=""
    while read -r k d t dp; do
        [[ -n "$k" && "$d" == "$fdev" ]] || continue
        [[ -z "$fallback" && "$k" == "${d}m"* ]] && fallback="$k $t $dp"
        if [[ "$fpath" == "$dp" || "$fpath" == "$dp"/* || "$dp" == / ]]; then
            if (( ${#dp} > bestlen )); then best="$k $t $dp"; bestlen=${#dp}; fi
        fi
    done <<< "$domains"
    # The fallback's own root is some other watched path on that filesystem;
    # label it with the file's mount point instead, so the page names where
    # the file is (the total, and so the percentage, are the same).
    if [[ -z "$best" && -n "$fallback" ]]; then
        local mnt
        mnt="$(timeout -k 1 2 stat -c %m -- "${fpath%/*}" 2>/dev/null)" || mnt=""
        [[ -n "$mnt" ]] && fallback="${fallback%% *} $(cut -d' ' -f2 <<< "$fallback") ${mnt}"
    fi
    printf '%s\n' "${best:-$fallback}"
}

_rw_holders() {
    # One line per distinct holder: "pid comm (parent ppid: comm)". Never the
    # command line: arguments can carry credentials, and the page leaves the
    # machine (review finding, P1).
    # Names come from the identity read through the holder's own /proc handle
    # (_RW_IDENT); without one, from comm with control bytes removed.
    local proc="${DG_PROC:-/proc}" pid comm ppid pcomm
    for pid in "$@"; do
        if [[ -n "${_RW_IDENT[$pid]:-}" ]]; then
            IFS=$'\t' read -r _ _ comm ppid pcomm <<< "${_RW_IDENT[$pid]}"
        else
            comm="$(cat "$proc/$pid/comm" 2>/dev/null)" || comm="?"
            ppid="$(awk '/^PPid:/ { print $2; exit }' "$proc/$pid/status" 2>/dev/null)" || ppid=""
            pcomm="?"
            [[ "$ppid" =~ ^[0-9]+$ ]] && pcomm="$(cat "$proc/$ppid/comm" 2>/dev/null)" || true
            comm="${comm//[[:cntrl:]]/?}"; pcomm="${pcomm//[[:cntrl:]]/?}"
        fi
        printf '  pid %s %s (parent %s: %s)\n' "$pid" "${comm:-?}" "${ppid:-?}" "${pcomm:-?}"
    done
}

_rw_episode_open() {
    # $1 generation, $2 now. The file's episode ended: page it again from here.
    unset "_RW_PAGED[$1]" "_RW_DUE[$1]" "_RW_DUE[${1}_observe]"
    _RW_EP[$1]="$2"
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
# finding: on a full disk the record is what fails). The lock is held from the
# first record to the last, taken for at most DG_RUNAWAY_LOCK_WAIT_S; when an
# operator's thaw or kill holds it, nothing is paused this poll. No child
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

_rw_respond() {
    # Pause the command writing a runaway task output. $1 generation, $2
    # dev:inode, $3 path, $4 size MB, rest: holder pids (identity in
    # _RW_IDENT). Sets _RW_PAUSED_NOW=1 when it paused something this poll and
    # _RW_ACTED to a one-line summary for the detection page.
    local gen="$1" key="$2" path="$3" size_mb="$4"
    shift 4
    _RW_PAUSED_NOW=0; _RW_ACTED=""
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
    # not a pause of the writer. A single-threaded zombie holds nothing.
    if [[ -z "$veto" ]]; then
        local blind brc="" b
        blind="$(_rw_blind)"
        in=""
        while read -r b pid; do
            if [[ "$b" == "#rc" ]]; then brc="$pid"; continue; fi
            [[ "$b" == "#blind" && "$pid" =~ ^[0-9]+$ ]] && in+="${pid} -"$'\n'
        done <<< "$blind"
        if [[ ! "$brc" =~ ^[01]$ ]]; then
            veto="the check for uninspectable processes did not complete"
        elif [[ -n "$in" ]]; then
            if ! _rw_chain <<< "$in"; then
                veto="the uninspectable processes' ancestry could not be read"
            else
                for pid in "${!_RW_CH[@]}"; do
                    IFS=$'\t' read -r _ status _ state threads _ _ anc <<< "${_RW_CH[$pid]}"
                    [[ "$status" == gone || "$status" == nocc || "$status" == claude ]] && continue
                    [[ "$state" == Z && "$threads" == 1 ]] && continue
                    if [[ "$status" != ok ]]; then
                        veto="pid ${pid} cannot be inspected and its ancestry cannot be read"; break
                    fi
                    for r in "${!roots[@]}"; do
                        [[ ",${anc}," == *",${r},"* ]] && { veto="pid ${pid}, in the same command, cannot be inspected"; break 2; }
                    done
                done
            fi
        fi
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
    wg_records_unlock "$lk"

    local all="${paused}${unconfirmed}"
    if [[ -z "$all" ]]; then
        log WARN "runaway ${path}: no writer could be paused (pid${refused:- none})"
        _RW_ACTED="no writer could be paused"
        return 0
    fi
    _RW_ACTED="paused"
    [[ -n "$newly" ]] || return 0   # nothing new this poll: the reminders cover it
    _RW_PAUSED_NOW=1
    local sid body title p
    sid="$(_rw_session_of "${path% (deleted)}")"
    body="${path} (${size_mb} MB) is growing fast in ${CC_TMP_DIR}."$'\n'
    [[ -n "$paused" ]] && body+="Paused (SIGSTOP, not killed): pid${paused}."$'\n'
    [[ -n "$unconfirmed" ]] && body+="SIGSTOP sent but NOT yet confirmed: pid${unconfirmed}. Recorded as pending; thaw resumes it and also cancels a stop that has not landed."$'\n'
    [[ -n "$refused" ]] && body+="Could not pause: pid${refused}."$'\n'
    [[ -n "$prestopped" ]] && body+="Already stopped by something else (not recorded, so thaw leaves it): pid${prestopped}."$'\n'
    body+="The Claude Code session itself was NOT paused; a tool call waiting on this command waits until it is resumed or until its own timeout. A paused command keeps any lock it holds."$'\n'
    body+="The output file was NOT emptied: it keeps its space until the command is resumed and finishes, or is ended."$'\n'
    [[ -n "$sid" ]] && body+="Claude Code session: ${sid}"$'\n'
    body+="Resume:"
    for p in $all; do body+=" scripts/watchgod thaw ${p};"; done
    body+=" or scripts/watchgod thaw all. A resumed command that is still growing fast while the disk is in trouble is paused again. End it instead:"
    for p in $all; do body+=" scripts/watchgod kill ${p};"; done
    title="Paused a runaway Claude Code task"
    [[ -n "$paused" ]] || title="Sent SIGSTOP to a runaway Claude Code task, NOT yet confirmed"
    log WARN "runaway ${path}: paused${paused:- none}; unconfirmed${unconfirmed:- none}; refused${refused:- none}"
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

wg_runaway_check() {
    # $1: the watched domains of this poll, one "key dev total_mb path" per
    # line, paths canonical (check_disks builds it). Pages each runaway file
    # once per mode.
    local domains="$1" now key size pid fd dev
    now="$(date +%s)"
    RUNAWAY_FAST=0
    _rw_flush_pending
    local -A size_of=() dev_of=() fds_of=()
    local scan scan_rc=""
    scan="$(_rw_scan)" || scan="#rc unknown"
    while read -r key size pid fd dev; do
        [[ -n "$key" ]] || continue
        if [[ "$key" == "#rc" ]]; then scan_rc="$size"; continue; fi
        size_of[$key]="$size"; dev_of[$key]="$dev"
        fds_of[$key]="${fds_of[$key]:-}${fds_of[$key]:+ }$pid/$fd"
    done <<< "$scan"
    # Paged (or between episodes) and not reported: follow it through the
    # descriptors it was last seen through. Its size comes from the stat below.
    local gen pf
    local -A follow=()
    for gen in "${!_RW_LINKS[@]}"; do
        [[ -n "${_RW_PAGED[$gen]:-}${_RW_EP[$gen]:-}" ]] || continue
        key="${gen%:*}"
        [[ -n "${size_of[$key]:-}" && -z "${follow[$key]:-}" ]] && continue
        # An inode reused while an older generation is still remembered has
        # two candidates: follow both, and let the stat's birth time say which
        # one the descriptor leads to (review finding).
        size_of[$key]=0; dev_of[$key]="${key%%:*}"
        for pf in ${_RW_LINKS[$gen]}; do
            [[ " ${fds_of[$key]:-} " == *" $pf "* ]] || fds_of[$key]="${fds_of[$key]:-}${fds_of[$key]:+ }$pf"
        done
        follow[$key]="${follow[$key]:-}${follow[$key]:+ }$gen"
    done
    local walk_ok=1
    if [[ ! "$scan_rc" =~ ^[01]$ ]]; then
        walk_ok=0
        log WARN "runaway-file walk of /proc did not complete (status ${scan_rc:-missing}; timeout ${DG_RUNAWAY_SCAN_TIMEOUT_S} s) — files it did not report were not checked this poll"
    fi

    local dt pg path dom dkey dtotal dpath size_mb raw_b norm_b why sid body grew
    local link id birth blk pfpid st_line st_rc=0 opened vlinks
    local -A size_now=() st_of=()
    local -a holders links=()
    pg="$(_wg_mode_tag)"
    # Every descriptor is re-checked in ONE bounded stat: a held file on a
    # network or FUSE mount that stalled after the walk must not hang the poll
    # loop (review finding). /proc readlink never touches the target's
    # filesystem; stat -L does. stat exits 1 when some link is gone (normal).
    for key in "${!fds_of[@]}"; do
        for pf in ${fds_of[$key]}; do links+=("${DG_PROC:-/proc}/${pf%/*}/fd/${pf#*/}"); done
    done
    if (( ${#links[@]} )); then
        while IFS=$'\t' read -r link st_line; do
            [[ -n "$link" ]] && st_of[$link]="$st_line"
        done < <(rc=0
                 timeout -k 2 "$DG_RUNAWAY_SCAN_TIMEOUT_S" stat -L -c $'%n\t%d:%i %W %b' -- "${links[@]}" 2>/dev/null || rc=$?
                 printf '#rc\t%s\n' "$rc")
        st_rc="${st_of["#rc"]:-unknown}"
        if [[ ! "$st_rc" =~ ^[01]$ ]]; then
            walk_ok=0
            log WARN "runaway-file revalidation did not complete (status ${st_rc}; timeout ${DG_RUNAWAY_SCAN_TIMEOUT_S} s) — files were not revalidated this poll (a killed stat loses its buffered output)"
        fi
    fi
    # Bind each holder still pointing at its file to the process behind it:
    # one python3 run per poll, under the same bound as the stat (it stats the
    # descriptors again). bound[key pid] marks a holder it confirmed. If it did
    # not run to completion, holders stay as the stat above found them.
    local -A bound=()
    local bound_ok=0 cand="" ncand=0 bk bp bst bstate bcomm bppid bpcomm
    _RW_IDENT=()
    for key in "${!fds_of[@]}"; do
        for pf in ${fds_of[$key]}; do
            read -r id _ <<< "${st_of["${DG_PROC:-/proc}/${pf%/*}/fd/${pf#*/}"]:-}" || id=""
            [[ "$id" == "$key" ]] || continue
            cand+="${key} ${pf%/*} ${pf#*/}"$'\n'
            ncand=$(( ncand + 1 ))
        done
    done
    # Never silent (review finding): a read that fails leaves the writers
    # unbound, and one that confirms nobody hides every file this poll.
    if [[ -n "$cand" ]]; then
        local idout id_rc=0
        if ! command -v python3 >/dev/null 2>&1; then
            _wg_warn_once rw_ident_nopy "runaway-file identity read skipped: no python3; writers are named without their start time"
        elif idout="$(timeout -k 2 "$DG_RUNAWAY_SCAN_TIMEOUT_S" python3 -I -S "$_RW_PROC_PY" "${DG_PROC:-/proc}" holders \
                          <<< "$cand" 2>/dev/null)"; then
            bound_ok=1
            while IFS=$'\t' read -r bk bp bst bstate bcomm bppid bpcomm; do
                [[ -n "$bk" && "$bp" =~ ^[0-9]+$ ]] || continue
                bound["$bk $bp"]=1
                _RW_IDENT[$bp]="${bst}"$'\t'"${bstate}"$'\t'"${bcomm}"$'\t'"${bppid}"$'\t'"${bpcomm}"
            done <<< "$idout"
            (( ${#bound[@]} )) || log WARN "runaway-file identity read confirmed none of the ${ncand} writers the stat found; the files only they hold are not checked this poll"
        else
            id_rc=$?
            _wg_warn_once "rw_ident_rc${id_rc}" "runaway-file identity read failed (status ${id_rc}; timeout ${DG_RUNAWAY_SCAN_TIMEOUT_S} s); writers are named without their start time"
        fi
    fi
    for key in "${!size_of[@]}"; do
        # Every descriptor is re-checked: since the walk, one may have been
        # closed, its number reused, or its process gone. A holder counts only
        # through a descriptor that still leads to the file measured, each
        # holder once (review findings). No such descriptor: skip the file.
        link=""; birth=""; blk=""; holders=(); vlinks=""
        local -A counted=()
        for pf in ${fds_of[$key]}; do
            pfpid="${pf%/*}"
            local l="${DG_PROC:-/proc}/${pfpid}/fd/${pf#*/}" b="" k=""
            read -r id b k <<< "${st_of[$l]:-}" || id=""
            [[ "$id" == "$key" ]] || continue
            # Not confirmed through its own /proc handle: closed, exited, or
            # its pid reused since the stat.
            (( bound_ok )) && [[ -z "${bound["$key $pfpid"]:-}" ]] && continue
            # A followed descriptor must still be a writer's: the same number
            # reopened read-only is not the writer that was paged (review
            # finding). The scan already checked its own.
            [[ -n "${follow[$key]:-}" ]] && ! _rw_writer "$pfpid" "${pf#*/}" && continue
            [[ -n "$link" ]] || { link="$l"; birth="$b"; blk="$k"; }
            # Once each: two generations' lists can name the same descriptor
            # (review finding: the copies grew by one every poll).
            [[ " $vlinks " == *" $pf "* ]] && continue
            vlinks+="${vlinks:+ }$pf"
            [[ -n "${counted[$pfpid]:-}" ]] && continue
            counted[$pfpid]=1
            holders+=("$pfpid")
        done
        unset counted
        [[ -n "$link" ]] || continue
        [[ "$birth" =~ ^[0-9]+$ ]] || birth=0
        gen="${key}:${birth}"
        if [[ -n "${follow[$key]:-}" ]]; then
            # Followed: only the same generation, and its size from the stat.
            [[ " ${follow[$key]} " == *" $gen "* && "$blk" =~ ^[0-9]+$ ]] || continue
            size_of[$key]=$(( blk * 512 ))
        fi
        _RW_LAST[$gen]=$now
        _RW_LINKS[$gen]="$vlinks"
        size_now[$gen]="${size_of[$key]}"
        # Down to half its size at the previous sighting since it was paged:
        # that episode is over. Back after a poll with NO writer seen (a
        # carried baseline: neither scanned nor followed), half of a small
        # baseline can never be seen (the scan stops at the floor), so it must
        # be at least halfway down from its baseline to the floor: a writer
        # that exits and a new one that truncates and refills is the case
        # (review finding); a file merely reopened a little smaller is not.
        opened=0
        if [[ -n "${_RW_PAGED[$gen]:-}" && -n "${_RW_PREV[$gen]:-}" ]] \
                && { (( ${size_of[$key]} * 2 <= ${_RW_PREV[$gen]} )) \
                     || { [[ -n "${_RW_PREV_AT[$gen]:-}" ]] \
                          && (( ${size_of[$key]} * 2 <= ${_RW_PREV[$gen]} + DG_RUNAWAY_MIN_MB * 1048576 )); }; }; then
            _rw_episode_open "$gen" "$now"
            opened=1
        fi
        # A followed file under the scan's floor only moves the baseline.
        [[ -n "${follow[$key]:-}" ]] && (( ${size_of[$key]} < DG_RUNAWAY_MIN_MB * 1048576 )) && continue
        path="$(readlink -- "$link" 2>/dev/null)" || path="?"
        dom="$(_rw_domain_for "${dev_of[$key]}" "${path% (deleted)}" "$domains")"
        [[ -n "$dom" ]] || continue   # not on a watched filesystem
        read -r dkey dtotal dpath <<< "$dom"
        [[ "$dtotal" =~ ^[0-9]+$ ]] && (( dtotal > 0 )) || continue
        size_mb=$(( ${size_of[$key]} / 1048576 ))
        raw_b=-1; norm_b=-1
        dt=$(( now - ${_RW_PREV_AT[$gen]:-$_RW_PREV_T} ))
        if [[ -n "${_RW_PREV[$gen]:-}" ]] && (( _RW_PREV_T > 0 && dt > 0 )); then
            raw_b=$(( ${size_of[$key]} - ${_RW_PREV[$gen]} ))
            (( raw_b >= 0 )) && norm_b=$(( raw_b * POLL_INTERVAL / dt ))
        fi
        why=""
        # Between episodes (opened, not yet paged), the size counts only on a
        # crossing: it was under the threshold at the previous sighting, in
        # the same whole MB as the test below (review finding: exact bytes here
        # could call a file "over" that the size test calls "under").
        local size_armed=1
        if [[ -n "${_RW_EP[$gen]:-}" && -z "${_RW_PAGED[$gen]:-}" && -n "${_RW_PREV[$gen]:-}" ]] \
                && (( ${_RW_PREV[$gen]} / 1048576 * 100 >= DG_RUNAWAY_PCT * dtotal )); then
            size_armed=0
        fi
        if (( size_armed && size_mb * 100 >= DG_RUNAWAY_PCT * dtotal )); then
            why="it alone uses $(( size_mb * 100 / dtotal ))% of ${dpath} (threshold ${DG_RUNAWAY_PCT}%)"
        fi
        grew=0
        if (( norm_b >= 0 && norm_b * 100 >= DG_RUNAWAY_RATE_PCT * dtotal * 1048576 )); then
            grew=1
            why="${why}${why:+; }it grew $(( raw_b / 1048576 )) MB in ${dt} s, $(( norm_b * 100 / (dtotal * 1048576) ))% of ${dpath} per ${POLL_INTERVAL} s (threshold ${DG_RUNAWAY_RATE_PCT}%)"
        fi
        if (( norm_b >= 0 && norm_b * 200 >= DG_RUNAWAY_RATE_PCT * dtotal * 1048576 )); then
            # shellcheck disable=SC2034  # read by check_disks in tmp_watchgod.sh
            RUNAWAY_FAST=1
        fi
        # Due in THIS mode only: an observe page never stands in for the one
        # an acting poll owes (review finding).
        if [[ -z "$why" && -n "${_RW_DUE[$gen$pg]:-}" ]]; then
            why="${_RW_DUE[$gen$pg]} (found at an earlier poll, whose page could not be queued)"
        fi
        [[ -n "$why" ]] || continue
        (( opened )) && continue
        # The response, every poll while it holds: only for a Claude Code task
        # output GROWING fast this poll (never on size alone, or a paused file
        # would be paused again after every thaw; never on a reason carried
        # from an earlier poll), only once its domain is ORANGE or RED.
        _RW_ACTED=""; _RW_PAUSED_NOW=0
        if (( grew )) && [[ "${RW_TIER[$dkey]:-}" == orange || "${RW_TIER[$dkey]:-}" == red ]] \
                && _rw_task_output "$path"; then
            if (( ! WATCHGOD_ACT )); then
                _wg_warn_once "rw_obs_${gen}" "OBSERVE: would pause the command writing ${path} (pid ${holders[*]})"
            elif (( ! walk_ok )); then
                log WARN "runaway ${path}: not paused this poll: the /proc walk did not complete"
            else
                _rw_respond "$gen" "$key" "$path" "$size_mb" "${holders[@]}"
            fi
        fi
        [[ " ${_RW_PAGED[$gen]:-} " == *" mode${pg} "* ]] && continue
        sid="$(_rw_session_of "${path% (deleted)}")"
        body="${path}: ${size_mb} MB in use, held open for writing. Why flagged: ${why}."$'\n'"Writers:"$'\n'"$(_rw_holders "${holders[@]:0:DG_RUNAWAY_MAX_HOLDERS}")"
        (( ${#holders[@]} > DG_RUNAWAY_MAX_HOLDERS )) && body+=$'\n'"  +$(( ${#holders[@]} - DG_RUNAWAY_MAX_HOLDERS )) more"
        [[ -n "$sid" ]] && body+=$'\n'"Claude Code session: ${sid}"
        local pkey="watchgod:runaway:${gen}" again=""
        if [[ -n "${_RW_EP[$gen]:-}" ]]; then
            local since
            since="$(date -d "@${_RW_EP[$gen]}" '+%Y-%m-%d %H:%M %Z' 2>/dev/null)" || since="epoch ${_RW_EP[$gen]}"
            pkey+=":e${_RW_EP[$gen]}"; again=" (again since ${since})"
            body+=$'\n'"It shrank after an earlier page (at ${since}) and is over the threshold again."
        fi
        if [[ "$_RW_ACTED" == paused ]]; then
            body+=$'\n'"The guardian paused the command writing it (its action page says how to resume or end it); the file was not emptied."
        elif [[ -n "$_RW_ACTED" ]]; then
            body+=$'\n'"The guardian did not pause it (${_RW_ACTED}). Stop the writer, or empty the file, before the filesystem fills."
        else
            body+=$'\n'"The guardian has not acted on it. Stop the writer, or empty the file, before the filesystem fills."
        fi
        if _wg_page critical "Runaway file on ${dpath}: ${path##*/}${again}" "$body" \
                "$pkey" "CRITICAL — runaway ${path} (${size_mb} MB)${again}"; then
            _RW_PAGED[$gen]="${_RW_PAGED[$gen]:-} mode${pg}"
            # Only this mode's debt: the other mode still owes its own page
            # (review finding).
            unset "_RW_DUE[$gen$pg]"
            log WARN "runaway file ${path} (${size_mb} MB; ${why}); writers: ${holders[*]}"
        else
            _RW_DUE[$gen$pg]="${why% (found at an earlier poll*}"
        fi
    done

    # Out of sight long enough: forgotten (DG_RUNAWAY_FORGET_S, or
    # DG_RUNAWAY_PAGED_FORGET_S once paged in this episode). Seen again later,
    # it is queued again under the same key, which the drainer collapses. Only
    # a complete walk proves a file absent: during an outage of the walk
    # nothing is forgotten, or the baselines it carries would expire (review
    # finding).
    local ttl
    if (( walk_ok )); then
        for gen in "${!_RW_LAST[@]}"; do
            ttl=$DG_RUNAWAY_FORGET_S
            # Paged, or between episodes: the drainer still holds the last key.
            [[ -n "${_RW_PAGED[$gen]:-}${_RW_EP[$gen]:-}" ]] && ttl=$DG_RUNAWAY_PAGED_FORGET_S
            (( now - ${_RW_LAST[$gen]} > ttl )) || continue
            unset "_RW_LAST[$gen]" "_RW_PAGED[$gen]" "_RW_EP[$gen]" "_RW_PREV[$gen]" "_RW_PREV_AT[$gen]" \
                  "_RW_LINKS[$gen]" "_RW_DUE[$gen]" "_RW_DUE[${gen}_observe]"
        done
    fi

    # A file this poll did not report keeps its baseline and its time until it
    # is forgotten, whether the walk completed or not: dropping it would make
    # its next sighting a first one, so a writer that closes its file for a
    # poll, or repeated incomplete walks, would hide a fast grower (#3024,
    # review finding).
    local -A carry=() carry_at=()
    for gen in "${!_RW_PREV[@]}"; do
        [[ -n "${size_now[$gen]:-}" ]] && continue
        carry[$gen]="${_RW_PREV[$gen]}"
        carry_at[$gen]="${_RW_PREV_AT[$gen]:-$_RW_PREV_T}"
    done
    _RW_PREV=(); _RW_PREV_AT=()
    for gen in "${!size_now[@]}"; do _RW_PREV[$gen]="${size_now[$gen]}"; done
    for gen in "${!carry[@]}"; do
        _RW_PREV[$gen]="${carry[$gen]}"; _RW_PREV_AT[$gen]="${carry_at[$gen]}"
    done
    _RW_PREV_T=$now
    _rw_repage "$now"
    return 0
}
