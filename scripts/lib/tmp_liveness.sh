# shellcheck shell=bash
# tmp_liveness.sh — "is anything still writing here?" helpers, shared by the
# tmp watchgod and disk_hygiene.sh's ~/tmp prune.
#
# Moved here verbatim from scripts/tmp_watchgod.sh when the watchgod stopped
# sweeping directories itself (v2, whole-disk guardian): disk_hygiene.sh became
# the one deleter, and its age-based prune needs the same liveness signal the
# watchgod's sweeps used, so a long-running download or unpack older than the
# age cut is never removed while a process still holds it open.
#
# Pure functions: nothing executed at source time. TL_PROC (default /proc)
# exists so tests can present a blind /proc.

glob_escape() {
    # Escape the four characters find(1)'s -path GLOB treats specially, so an
    # exclusion built from a real directory name matches that name literally.
    # MEASURED 2026-09-23: without this, a directory named `sp[a]re` is NOT
    # excluded by -not -path ".../sp[a]re/*" and is reaped anyway — a silent
    # fail-OPEN in the exact direction this guard exists to prevent.
    printf '%s' "$1" | sed 's/[][*?\\]/\\&/g'
}

canon_dir() {
    # Collapse trailing slashes so a configured path and the paths find(1)
    # emits under it agree. GNU find reproduces its START POINT verbatim,
    # repeated slashes included — MEASURED: `find "$root//" -maxdepth 1`
    # emits `$root//child`. Normalising only the EXCLUSION side therefore
    # relocates the mismatch instead of closing it, which is why this is
    # applied to the value where it ENTERS rather than at each consumer.
    local d="$1"
    while [[ "$d" == */ && "$d" != "/" ]]; do d="${d%/}"; done
    printf '%s' "$d"
}

live_open_paths() {
    # Every filesystem path a live process currently holds OPEN, one per line.
    #
    # This is the signal the reap needs, and mtime is not: a directory being
    # written RIGHT NOW is indistinguishable by mtime from a cache written a
    # second ago and already closed, so a recency threshold either destroys
    # in-flight work or refuses to reclaim fresh junk.
    #
    # It is a STRICTLY BETTER signal, not a complete one. A writer that opens,
    # writes and closes each file in turn — a shell loop calling curl per file,
    # an extractor that closes each member, a write-then-rename — holds no
    # descriptor at the instant of the sweep and is reaped exactly as before.
    # Do not read this as a biconditional.
    #
    # NEWLINE-delimited, and that is a CONSTRAINT rather than a choice. A path
    # containing a newline splits into two records, neither of which matches —
    # a silent fail-OPEN. The obvious fix is NUL delimiting, and it is not
    # available here: MEASURED 2026-09-23, bash command substitution STRIPS NUL
    # bytes outright (it warns "ignored null byte in input"), so a NUL-delimited
    # snapshot arrives in the variable as one concatenated blob with no
    # separators at all — strictly worse. awk itself handles RS="\0" fine; the
    # shell variable is the limit. Accepted: every directory name this sweep
    # meets in practice comes from mktemp, pip, or a CC session UUID.
    #
    # Same-uid processes only: another uid's /proc/<pid>/fd is EACCES and is
    # skipped. cc-tmp is mode 700, so its writers are normally this user's own
    # processes — "normally" because a root process writing there would be
    # invisible here, which is one of the states the caller's self-test exists
    # to notice.
    #
    # One find for the whole sweep rather than one per candidate directory.
    # MEASURED 2026-09-23 on a live install: ~2,000 descriptors across ~170
    # processes in a single pass (47-118ms), and identical counts inside a
    # transient systemd --user unit (the service's own context) as in an
    # interactive shell — this unit sets no ProtectProc/ProcSubset.
    #
    # Plus every process's WORKING DIRECTORY: a job running FROM a directory
    # (a script started in ~/tmp/<job>) may hold no descriptor inside it at the
    # instant of a sweep, yet deleting the directory out from under it breaks
    # every relative path it uses next.
    local proc="${TL_PROC:-/proc}"
    {
        find "$proc"/[0-9]*/fd -maxdepth 1 -type l -printf '%l\n'
        find "$proc"/[0-9]*/cwd -maxdepth 0 -type l -printf '%l\n'
    } 2>/dev/null || true
}

_tl_unescape() {
    # Decode one mountinfo field into the variable named $1. The kernel writes
    # every escape as exactly three octal digits (\040 \011 \012 \134), but
    # `printf %b` reads \0NNN as \0 plus up to THREE more digits, so "\0401"
    # (a space, then "1") decodes to garbage and a mount named "job 1" would
    # slip past the guard. Rewriting each "\" as "\0" makes %b consume exactly
    # the kernel's three digits. Safe because the kernel escapes every
    # backslash, so each one in the field starts an escape.
    printf -v "$1" '%b' "${2//\\/\\0}"
}

mount_targets() {
    # Mount points from the kernel's mount table, one per line, as field 5 of
    # /proc/self/mountinfo: space, tab, newline and backslash escaped as \NNN
    # octal (\040 \011 \012 \134), so every backslash in a field starts an
    # escape and a newline in a name cannot split a record. path_crosses_mount
    # decodes them. Read directly rather than through findmnt, so the guard
    # needs no util-linux and uses the same source and escape format as
    # disk_reclaim.py's _mount_points (premise check on #2570: two formats is
    # where the round-1 escape finding came from, and a missing findmnt made
    # "unreadable" a permanent state on some hosts). TL_MOUNTINFO exists so
    # tests can present a crafted table.
    #
    # With $1 (a canonical ROOT), only the mounts strictly below ROOT: a
    # deleter under ROOT never needs the rest, and filtering once keeps the
    # per-candidate check O(mounts under ROOT), usually zero.
    #
    # Returns 1 when the table cannot be read, or has no "/" entry (every real
    # table does), so a caller never mistakes "unreadable" for "no mounts".
    # mapfile reads the file with no temp file and no fork.
    local root="${1:-}" src="${TL_MOUNTINFO:-/proc/self/mountinfo}" line t seen_root=0 filter=0
    local -a fields lines
    [[ -n "$root" ]] && filter=1
    root="${root%/}"  # "/" becomes "", so the pattern below is "/*", not "//*"
    mapfile -t lines 2>/dev/null < "$src" || return 1
    for line in "${lines[@]}"; do
        read -r -a fields <<< "$line"
        (( ${#fields[@]} > 4 )) || continue
        _tl_unescape t "${fields[4]}"
        [[ "$t" == / ]] && seen_root=1
        # STRICTLY below ROOT ("/" itself is not below "/").
        if (( ! filter )) || [[ "$t" == "$root"/* && "$t" != "${root:-/}" ]]; then
            printf '%s\n' "${fields[4]}"
        fi
    done
    (( seen_root ))
}

path_crosses_mount() {
    # 0 when a mount in $2 (mount_targets output, read once by the caller and
    # already limited to mounts strictly below the caller's ROOT) is EQUAL to
    # $1, BELOW it, or ABOVE it. Every candidate lies under ROOT, so those
    # three relations are the only ways a mount under ROOT can meet it; any
    # other mount is disjoint and rm of $1 never reaches it. ABOVE matters
    # where a walk descends past a mount: the cc-tmp sweep emits
    # claude-<uid>/<project>/<session>, and a mount at <project> put every
    # session in it inside the mount, where --one-file-system cannot help
    # (review finding on #2570, round 3). A device-number
    # comparison misses a bind mount and an incus dir-pool volume, which keep
    # their parent's device (review finding on #2521 item 6); the table lists
    # every mount, same device or not. Each entry is DECODED and compared raw,
    # rather than encoding $1 to match: an encoder must know the whole escape
    # set, and every byte it missed was a way past this guard (#2570).
    local p="$1" targets="${2:-}" line t
    [[ -n "$targets" ]] || return 1
    while IFS= read -r line; do
        _tl_unescape t "$line"
        [[ "$t" == "$p" || "$t" == "$p"/* || "$p" == "$t"/* ]] && return 0
    done < <(printf '%s\n' "$targets")
    return 1
}

tree_holds_mount() {
    # 0 when $1 is, or holds, a mount — or when that cannot be known ($3 != 1:
    # the table was unreadable), so a caller that skips its own table check
    # still fails closed. $2/$3 = mount_targets output and whether it was
    # readable. Callers refuse a whole pass on an unreadable table, with a
    # reason of their own, before reaching this (#2570 premise check: a
    # per-candidate refusal read as "kept N" on the page, which looks exactly
    # like sparing real mounts).
    local p="$1" mounts="${2:-}" table_ok="${3:-0}"
    [[ "$table_ok" == 1 ]] || return 0
    [ -e "$p" ] || return 1
    path_crosses_mount "$p" "$mounts"
}

remove_tree_one_fs() {
    # Recursively remove $1 unless it is, or holds, a mount — the one way
    # every recursive deleter in the guardian's SHELL scripts removes a tree
    # (review finding on #2570: the guard existed in two of four).
    # disk_reclaim.py's rmtree carries its own mount check. Arguments as for
    # tree_holds_mount; the table is the one the caller read at the start of
    # its pass, not re-read here. Returns 0 when $1 is gone, 2 when it was
    # spared, 1 when removal failed and it is still there. --one-file-system
    # stays as a backstop for a mount that appears mid-pass on another device.
    # Accepted residual: a SAME-device bind mount created between the table
    # read and this rm is not seen. Creating one needs CAP_SYS_ADMIN, which
    # nothing this guardian protects against holds without also being able to
    # delete the data directly.
    if tree_holds_mount "$@"; then
        return 2
    fi
    local p="$1"
    rm -rf --one-file-system -- "$p" 2>/dev/null
    if [ -e "$p" ] || [ -L "$p" ]; then return 1; fi
    return 0
}

liveness_visible() {
    # 0 when this process can see the descriptors of at least one OTHER
    # process — the precondition for reading "nothing holds it" as evidence.
    # Every deleter that consults live_open_paths checks this FIRST and refuses
    # when it fails (review finding: a blind scan read as "nothing is held"
    # would let an age prune remove a live job).
    #
    # Why "another process" rather than "the snapshot is non-empty": the
    # snapshot's own find runs in a subshell whose cwd and descriptors are
    # visible, so an empty snapshot is essentially impossible even with /proc
    # hidden. What this cannot see: another uid's processes (hidepid,
    # ProtectProc=invisible, or plain EACCES). That was always the model —
    # the swept trees are this user's own, written by this user's processes.
    #
    # The caller's own DESCENDANTS do not count either (#2515), and neither do
    # zombies (no descriptors to see). In a private PID namespace /proc shows
    # only the caller's own tree, and a live child of its own — the watchgod's
    # backgrounded du, a pipeline stage — would otherwise read as "visible".
    # The caller's ANCESTORS do count (its parent is the user's service
    # manager): under the same-uid model, seeing their descriptors is exactly
    # the evidence wanted.
    local proc="${TL_PROC:-/proc}" p pid
    for p in "$proc"/[0-9]*; do
        pid="${p##*/}"
        [[ "$pid" == "$$" || "$pid" == "$BASHPID" ]] && continue
        [[ -r "$p/fd" && -x "$p/fd" ]] || continue
        # 0 = ours or a zombie, 2 = could not classify (it vanished mid-check):
        # neither is evidence of another live process, so neither counts.
        _tl_own_or_zombie "$proc" "$pid"
        case $? in 0|2) continue ;; esac
        return 0
    done
    return 1
}

_tl_status_field() {
    # Echo field $2 (State or PPid) of $1 (a /proc/<pid>/status path); rc 1
    # when unreadable or absent.
    local line
    [[ -r "$1" ]] || return 1
    while IFS= read -r line; do
        if [[ "$line" =~ ^$2:[[:space:]]+([^[:space:]]+) ]]; then
            printf '%s' "${BASH_REMATCH[1]}"
            return 0
        fi
    done < "$1" 2>/dev/null
    return 1
}

_tl_own_or_zombie() {
    # 0 when pid $2 (under proc root $1) is a zombie or a descendant of this
    # shell ($$ or $BASHPID). 1 when it is positively ANOTHER process: its chain
    # ends at PPid 0 or 1 outside our tree, or runs past 64 hops. 2 when it
    # cannot be classified: a status file that will not read, here or partway
    # up the chain. The caller has already read the pid's fd/, so same-uid
    # access is proven and an unreadable status means the process (or an
    # ancestor) exited mid-check; that proves nothing, so the caller skips it
    # rather than counting it (counting it was a fail-open in exactly the
    # private-namespace case this exists for, review of #2515). The match
    # against $$ is checked before the PPid<=1 stop, because in a private PID
    # namespace the caller itself can be pid 1.
    local proc="$1" pid="$2" state ppid hops=0
    state=$(_tl_status_field "$proc/$pid/status" State) || return 2
    [[ "$state" == Z* ]] && return 0
    ppid=$(_tl_status_field "$proc/$pid/status" PPid) || return 2
    while (( hops < 64 )); do
        [[ "$ppid" =~ ^[0-9]+$ ]] || return 2
        [[ "$ppid" == "$$" || "$ppid" == "$BASHPID" ]] && return 0
        (( ppid <= 1 )) && return 1
        ppid=$(_tl_status_field "$proc/$ppid/status" PPid) || return 2
        hops=$(( hops + 1 ))
    done
    return 1
}

dir_has_live_writer() {
    # 0 when the snapshot ($2) holds an open path under directory $1. Used for
    # the depth-1 REAP decision, where the unit is the whole directory: reaping
    # only the quiet part of a tree being written leaves its writer a
    # partially-deleted directory.
    #
    # The needle goes through the environment rather than `awk -v`, which
    # expands backslash escapes in the value — MEASURED: a directory named
    # `ta\tb` silently fails to match under -v, and fail-OPEN follows.
    # Prefix-matched on "$1/" so a sibling sharing a name prefix
    # (pip-unpack-a beside pip-unpack-abc) cannot match the wrong directory.
    local dir="$1" snapshot="$2"
    [[ -n "$snapshot" ]] || return 1
    # No early exit on match, deliberately: awk quitting mid-stream leaves
    # printf writing into a closed pipe, and under pipefail the pipeline then
    # returns 141 (SIGPIPE) — which the caller reads as "no live writer", so a
    # FOUND writer produced a reap. MEASURED 2026-09-23 at snapshots >~379KB
    # (mawk's read buffer; implementation-dependent). Draining the whole
    # stream costs ~60ms at 800KB and makes the status always awk's own.
    #
    # The " (deleted)" skip: an unlinked inode's directory reclaims nothing
    # and would be spared forever. A real filename ending in that literal
    # string is indistinguishable (/proc does not escape) and would hide its
    # writer — accepted: cc-tmp is mode 700 and same-uid, so an actor who can
    # craft that name can already delete the tree directly.
    printf '%s\n' "$snapshot" | _wg_needle="$dir/" awk '
        / \(deleted\)$/ { next }
        index($0, ENVIRON["_wg_needle"]) == 1 { found = 1 }
        $0 "/" == ENVIRON["_wg_needle"] { found = 1 }   # a cwd AT the directory
        END { exit !found }
    '
}

path_is_held() {
    # 0 when the snapshot ($2) holds exactly path $1 open (a loose file).
    #
    # The whole stream is drained, never `grep -q`: an early exit leaves printf
    # writing into a closed pipe, and under pipefail the SIGPIPE status (141)
    # reads as "not held". MEASURED 2026-09-26 on a busy box: a held file was
    # DELETED this way once the snapshot outgrew the pipe buffer — the same
    # trap dir_has_live_writer documents above.
    local path="$1" snapshot="$2"
    [[ -n "$snapshot" ]] || return 1
    printf '%s\n' "$snapshot" | _wg_exact="$path" awk '
        $0 == ENVIRON["_wg_exact"] { found = 1 }
        END { exit !found }
    '
}
