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

mount_targets() {
    # Mount points from the MOUNT TABLE, one per line, in findmnt -r's raw
    # form: every unsafe byte, newline included, escaped as \xNN, and every
    # backslash as \x5c (measured on util-linux 2.39.3 with crafted names —
    # `\c`, `\n`, `\x41`, a trailing backslash; review of #2570). Lines stay
    # escaped so a newline in a name cannot split a record; path_crosses_mount
    # decodes them.
    #
    # With $1 (a canonical ROOT), only the mounts strictly below ROOT: a
    # deleter under ROOT never needs the rest, and filtering once keeps the
    # per-candidate check O(mounts under ROOT) — usually zero — instead of
    # O(whole table) per candidate (review: ~29 ms per candidate at 500 mounts).
    #
    # Returns 1 when the table could not be read — findmnt failed (a table
    # it abandoned partway must not pass as complete), or no line decodes to
    # "/", which every real table has — so a caller never mistakes
    # "unreadable" for "no mounts". Captured by command substitution and fed
    # back through a pipe, never a here-string: bash spools a large
    # here-string to a temp file, which fails on a full disk.
    local root="${1:-}" line t seen_root=0 out
    root="${root%/}"  # a root of "/" would otherwise match nothing ("//*")
    out="$(findmnt -rn -o TARGET 2>/dev/null)" || return 1
    while IFS= read -r line; do
        printf -v t '%b' "$line"
        [[ "$t" == / ]] && seen_root=1
        if [[ -z "$root" || "$t" == "$root"/* ]]; then
            printf '%s\n' "$line"
        fi
    done < <(printf '%s\n' "$out")
    (( seen_root ))
}

path_crosses_mount() {
    # 0 when $1 is a mount point, or has a mount somewhere below it.
    # $2 = mount_targets output (computed once by the caller), $3 = 1 when
    # that table was read successfully. A device-number comparison alone
    # misses a bind mount and an incus dir-pool volume, which keep their
    # parent's device (review finding on #2521 item 6).
    #
    # Each entry is DECODED and compared raw, rather than encoding $1 to
    # match: an encoder has to know findmnt's whole escape set, and every byte
    # it missed (a newline, any control byte) was a way past this guard
    # (review findings on #2570). A readable table decides alone — it holds a
    # mount AT the path too; `mountpoint` (one fork per candidate) is the
    # fallback only when the table could not be read.
    local p="$1" targets="${2:-}" table_ok="${3:-0}" line t
    if [[ "$table_ok" != 1 ]]; then
        mountpoint -q -- "$p" 2>/dev/null && return 0
    fi
    [[ -n "$targets" ]] || return 1
    while IFS= read -r line; do
        printf -v t '%b' "$line"
        [[ "$t" == "$p" || "$t" == "$p"/* ]] && return 0
    done < <(printf '%s\n' "$targets")
    return 1
}

tree_holds_mount() {
    # 0 when $1 exists and is, or holds, a separate mount. $2 = device number
    # of the tree being pruned, $3/$4 = mount_targets output and whether it
    # was readable (all computed once by the caller). A readable table lists
    # every mount, separate device or not, so it decides alone and costs no
    # fork per candidate; only an unreadable table falls back to the device
    # comparison and `mountpoint` (review of #2570: forks per candidate made a
    # 1,000-unit sweep take seconds).
    local p="$1" dev="$2" mounts="${3:-}" table_ok="${4:-0}"
    [ -e "$p" ] || return 1
    if [[ "$table_ok" == 1 ]]; then
        path_crosses_mount "$p" "$mounts" 1
        return
    fi
    [ "$(stat -c %d -- "$p" 2>/dev/null)" != "$dev" ] || path_crosses_mount "$p" "$mounts" 0
}

remove_tree_one_fs() {
    # Recursively remove $1 unless it is, or holds, a separate mount — the one
    # way every recursive deleter in the guardian's SHELL scripts removes a
    # tree (review finding on #2570: the guard existed in two of four).
    # disk_reclaim.py's rmtree carries its own mount check. Arguments as for
    # tree_holds_mount. Returns 0 when $1 is gone, 2 when it was spared as a
    # mount, 1 when removal failed and it is still there. --one-file-system
    # stays as a backstop; it cannot see a same-device bind mount, which is
    # why the table check comes first.
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
    local proc="${TL_PROC:-/proc}" p
    for p in "$proc"/[0-9]*; do
        [[ "${p##*/}" == "$$" || "${p##*/}" == "$BASHPID" ]] && continue
        [[ -r "$p/fd" && -x "$p/fd" ]] && return 0
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
