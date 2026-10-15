# shellcheck shell=bash
# The guardian's reserve files for tmp_watchgod.sh: space set aside while a
# filesystem is healthy and released at RED, so the last MB are there for
# recovery rather than for whatever is filling the disk. $HOME's reserve
# lives in DG_STATE_DIR; cc-tmp's own (smaller) one in cc-tmp itself when
# cc-tmp is its own volume. Sourced by tmp_watchgod.sh after
# disk_guardian.sh (DG_STATE_DIR, DG_ORANGE_PCT); log, _wg_warn_once and
# WATCHGOD_ACT are the daemon's. Nothing runs at source time but the
# assignments below.

RESERVE_FILE="$DG_STATE_DIR/reserve"

reserve_ensure() {
    # Create the reserve on the $HOME filesystem when it is healthy. $1 free
    # MB, $2 total MB. Never takes the filesystem below its ORANGE floor.
    local free="$1" total="$2" size orange_floor
    [[ -f "$RESERVE_FILE" ]] && return 0
    size=$(( total / 100 ))
    (( size > RESERVE_MAX_MB )) && size=$RESERVE_MAX_MB
    (( size >= 64 )) || return 0
    orange_floor=$(( total * DG_ORANGE_PCT / 100 ))
    (( free - size > orange_floor )) || return 0
    if (( WATCHGOD_ACT == 0 )); then
        _wg_warn_once reserve_obs "OBSERVE: would create a ${size} MB reserve file at ${RESERVE_FILE}"
        return 0
    fi
    mkdir -p "$DG_STATE_DIR" 2>/dev/null || true
    if fallocate -l "${size}M" "$RESERVE_FILE" 2>/dev/null; then
        log INFO "reserve file created: ${size} MB at ${RESERVE_FILE} (released at RED)"
    else
        rm -f "$RESERVE_FILE" 2>/dev/null || true
        _wg_warn_once reserve_fail "reserve file could not be preallocated (fallocate unsupported here?) — RED has no reserve to release"
    fi
    return 0
}

reserve_release() {
    [[ -f "$RESERVE_FILE" ]] || { echo "none held"; return 0; }
    local sz
    sz=$(( $(stat -c %s -- "$RESERVE_FILE" 2>/dev/null || echo 0) / 1048576 ))
    if (( WATCHGOD_ACT == 0 )); then
        echo "OBSERVE: would release ${sz} MB"
        return 0
    fi
    if rm -f -- "$RESERVE_FILE" 2>/dev/null && [[ ! -e "$RESERVE_FILE" ]]; then
        log INFO "reserve file released: ${sz} MB at ${RESERVE_FILE}"
        echo "released ${sz} MB (a snapshot may still pin its blocks; the next poll measures what was actually freed)"
        return 0
    fi
    # Never "released" when it was not: the page would claim space that is
    # still held.
    log WARN "reserve file ${RESERVE_FILE} could not be removed at RED; retried every RED poll"
    echo "NOT released: ${RESERVE_FILE} could not be removed (read-only or protected?); retried every RED poll"
    return 1
}

# cc-tmp's own reserve. cc-tmp is usually its own quota volume, so the $HOME
# reserve frees nothing there; without one, the last few MB a session's Bash
# needs (its per-call cwd file) go to whatever is filling the volume. Small on
# purpose: it buys the seconds the runaway response needs, not a workspace.
# min(128 MB, 5% of the volume); the retention sweep never reaps it.
CC_RESERVE_NAME=".watchgod-reserve"

cc_reserve_ensure() {
    # $1 free MB, $2 total MB of cc-tmp's domain, when it is GREEN.
    local free="$1" total="$2" size yellow_floor f="$CC_TMP_DIR/$CC_RESERVE_NAME"
    # A link where the reserve goes is not the reserve: fallocate would follow
    # it out of cc-tmp, and a release would remove only the link (review
    # finding). Removed, never followed.
    [[ -L "$f" ]] && { rm -f -- "$f" 2>/dev/null || true; return 0; }
    [[ -e "$f" ]] && return 0
    size=$(( total * 5 / 100 ))
    (( size > 128 )) && size=128
    (( size >= 16 )) || return 0
    # Never below the YELLOW line: cc-tmp's tier sheds routing call sites at
    # YELLOW, and the guardian's own file must not hold it there (review
    # finding).
    yellow_floor=$(( total * DG_YELLOW_PCT / 100 ))
    (( free - size > yellow_floor )) || return 0
    if (( WATCHGOD_ACT == 0 )); then
        _wg_warn_once cc_reserve_obs "OBSERVE: would create a ${size} MB reserve file at ${f}"
        return 0
    fi
    if fallocate -l "${size}M" "$f" 2>/dev/null; then
        log INFO "cc-tmp reserve created: ${size} MB at ${f} (released at cc-tmp RED)"
    else
        rm -f -- "$f" 2>/dev/null || true
        _wg_warn_once cc_reserve_fail "cc-tmp reserve could not be preallocated at ${f}; cc-tmp RED has no reserve to release"
    fi
    return 0
}

cc_reserve_release() {
    local f="$CC_TMP_DIR/$CC_RESERVE_NAME" sz
    if [[ -L "$f" ]]; then
        rm -f -- "$f" 2>/dev/null || true
        echo "none held (a link in its place was removed)"
        return 0
    fi
    [[ -f "$f" ]] || { echo "none held"; return 0; }
    sz=$(( $(stat -c %s -- "$f" 2>/dev/null || echo 0) / 1048576 ))
    if (( WATCHGOD_ACT == 0 )); then
        echo "OBSERVE: would release ${sz} MB"
        return 0
    fi
    if rm -f -- "$f" 2>/dev/null && [[ ! -e "$f" ]]; then
        log INFO "cc-tmp reserve released: ${sz} MB at ${f}"
        echo "released ${sz} MB"
        return 0
    fi
    log WARN "cc-tmp reserve ${f} could not be removed at RED; retried every RED poll"
    echo "NOT released: ${f} could not be removed (read-only or protected?); retried every RED poll"
    return 1
}

_wg_fs_id() {
    # The filesystem a statvfs of $1 measures: a btrfs filesystem by its UUID
    # (every subvolume of one pool reports the same pool), anything else by
    # its device number.
    local mline fstype source uuid
    if mline="$(dg_mount_line "$1")"; then
        IFS=$'\t' read -r _ fstype source _ <<< "$mline"
        if [[ "$fstype" == btrfs ]] && uuid="$(dg_btrfs_uuid "$source")"; then
            printf 'btrfs:%s' "$uuid"
            return 0
        fi
    fi
    printf 'dev:%s' "$(stat -c %d -- "$1" 2>/dev/null)"
}

_wg_reserve_mb() {
    # MB allocated to the guardian's reserve files ($HOME's and cc-tmp's) that
    # the measurement of the domain at path $1 includes; $2 is how it is
    # measured: "quota" counts its own subvolume (same device number), "fs"
    # its whole filesystem (same filesystem). The growth rate discounts a
    # change in them, so creating or releasing a reserve does not read as the
    # disk filling or emptying in ANY domain that sees it (review findings: a
    # false time-to-full; a reserve counted only in its owning domain).
    local path="$1" binding="${2:-fs}" f mb=0 b want=""
    for f in "$RESERVE_FILE" "$CC_TMP_DIR/$CC_RESERVE_NAME"; do
        [[ -f "$f" && ! -L "$f" ]] || continue
        if [[ "$binding" == quota ]]; then
            [[ "$(stat -c %d -- "$f" 2>/dev/null)" == "$(stat -c %d -- "$path" 2>/dev/null)" ]] || continue
        else
            [[ -n "$want" ]] || want="$(_wg_fs_id "$path")"
            [[ "$(_wg_fs_id "$f")" == "$want" ]] || continue
            # The same filesystem as statvfs sees it: a project-quota volume
            # shares its device with the root filesystem but statvfs-reports
            # only itself, so it must not count the root's reserve (review
            # finding; the same rule that keys such a domain apart).
            local sa sb
            read -r sa sb < <(dg_raw_sizes_mb "${f%/*}" "$path")
            dg_same_size "$sa" "$sb" || continue
        fi
        b="$(stat -c %b -- "$f" 2>/dev/null)" || continue
        [[ "$b" =~ ^[0-9]+$ ]] && mb=$(( mb + b * 512 / 1048576 ))
    done
    printf '%s' "$mb"
}

# Domains the runaway response relieved this poll (domain key -> 1): it emptied
# a file there. A RED lever that would spend the last reserve waits a poll.
# shellcheck disable=SC2034  # set by wg_runaway_check, read by handle_fs
declare -gA RUNAWAY_RELIEVED=()
