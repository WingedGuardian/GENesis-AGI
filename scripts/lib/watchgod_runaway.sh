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

_rw_show() {
    # $1 made safe for a page or a log line: a file name is chosen by whoever
    # created the file, and a newline, a Unicode line separator or a bidi
    # control in it could forge or disguise page lines (review finding). The
    # same sanitiser as the process names; printable ASCII only without it.
    local out
    if out="$(printf '%s' "$1" | timeout -k 1 5 python3 -I -S "$_RW_PROC_PY" - safe 2>/dev/null)" \
            && [[ -n "$out" ]]; then
        printf '%s' "$out"
    else
        printf '%s' "$1" | LC_ALL=C tr -c '[:print:]' '?'
    fi
}

_rw_holders() {
    # One line per distinct holder: "pid comm (parent ppid: comm)". Never the
    # command line: arguments can carry credentials, and the page leaves the
    # machine (review finding, P1).
    # Names come from the identity read through the holder's own /proc handle
    # (_RW_IDENT); without one, from comm with everything but printable
    # ASCII replaced (any locale: a multibyte line separator or bidi control
    # must not reach the page either).
    local proc="${DG_PROC:-/proc}" pid comm ppid pcomm
    for pid in "$@"; do
        if [[ -n "${_RW_IDENT[$pid]:-}" ]]; then
            IFS=$'\t' read -r _ _ comm ppid pcomm <<< "${_RW_IDENT[$pid]}"
        else
            comm="$(cat "$proc/$pid/comm" 2>/dev/null)" || comm="?"
            ppid="$(awk '/^PPid:/ { print $2; exit }' "$proc/$pid/status" 2>/dev/null)" || ppid=""
            pcomm="?"
            [[ "$ppid" =~ ^[0-9]+$ ]] && pcomm="$(cat "$proc/$ppid/comm" 2>/dev/null)" || true
            comm="$(printf '%s' "$comm" | LC_ALL=C tr -c '[:print:]' '?')"
            pcomm="$(printf '%s' "$pcomm" | LC_ALL=C tr -c '[:print:]' '?')"
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

wg_runaway_check() {
    # $1: the watched domains of this poll, one "key dev total_mb path" per
    # line, paths canonical (check_disks builds it). Pages each runaway file
    # once per mode.
    local domains="$1" now key size pid fd dev
    now="$(date +%s)"
    RUNAWAY_FAST=0
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

    local dt pg path dom dtotal dpath size_mb raw_b norm_b why sid body
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
        read -r _ dtotal dpath <<< "$dom"
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
        if (( norm_b >= 0 && norm_b * 100 >= DG_RUNAWAY_RATE_PCT * dtotal * 1048576 )); then
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
        [[ " ${_RW_PAGED[$gen]:-} " == *" mode${pg} "* ]] && continue
        # The raw path stays the key for the domain and the session; only
        # what is shown goes through the sanitiser.
        local spath
        spath="$(_rw_show "$path")"
        sid="$(_rw_session_of "${path% (deleted)}")"
        [[ -n "$sid" ]] && sid="$(_rw_show "$sid")"
        body="${spath}: ${size_mb} MB in use, held open for writing. Why flagged: ${why}."$'\n'"Writers:"$'\n'"$(_rw_holders "${holders[@]:0:DG_RUNAWAY_MAX_HOLDERS}")"
        (( ${#holders[@]} > DG_RUNAWAY_MAX_HOLDERS )) && body+=$'\n'"  +$(( ${#holders[@]} - DG_RUNAWAY_MAX_HOLDERS )) more"
        [[ -n "$sid" ]] && body+=$'\n'"Claude Code session: ${sid}"
        local pkey="watchgod:runaway:${gen}" again=""
        if [[ -n "${_RW_EP[$gen]:-}" ]]; then
            local since
            since="$(date -d "@${_RW_EP[$gen]}" '+%Y-%m-%d %H:%M %Z' 2>/dev/null)" || since="epoch ${_RW_EP[$gen]}"
            pkey+=":e${_RW_EP[$gen]}"; again=" (again since ${since})"
            body+=$'\n'"It shrank after an earlier page (at ${since}) and is over the threshold again."
        fi
        body+=$'\n'"The guardian has not acted on it. Stop the writer, or empty the file, before the filesystem fills."
        if _wg_page critical "Runaway file on ${dpath}: ${spath##*/}${again}" "$body" \
                "$pkey" "CRITICAL — runaway ${spath} (${size_mb} MB)${again}"; then
            _RW_PAGED[$gen]="${_RW_PAGED[$gen]:-} mode${pg}"
            # Only this mode's debt: the other mode still owes its own page
            # (review finding).
            unset "_RW_DUE[$gen$pg]"
            log WARN "runaway file ${spath} (${size_mb} MB; ${why}); writers: ${holders[*]}"
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
    return 0
}
