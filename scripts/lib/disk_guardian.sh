# shellcheck shell=bash
# disk_guardian.sh — whole-disk measurement and tiering for the tmp watchgod.
#
# The watchgod's job is to keep the disk from filling. A full disk is the one
# failure that takes every session and service down at once: on btrfs or on an
# LVM thin pool the filesystem forces itself read-only, and inside a container
# with a btrfs quota every writer gets "Disk quota exceeded". Either way nothing
# can commit until someone frees space.
#
# "How much room is left" has more than one answer, and the SMALLEST one is the
# one that bites first. This file reads each of them and takes the minimum:
#
#   1. statvfs (what df shows).
#   2. The btrfs QUOTA on the subvolume holding the path. statvfs cannot see it:
#      MEASURED 2026-09-26 on a live install, df reported 208 GB free while the
#      container's own subvolume sat at 140 GB of a 280 GB quota. On such a box
#      every df-based threshold trips ~68 GB late.
#   3. btrfs UNALLOCATED space and METADATA headroom. btrfs can run out of
#      metadata and go read-only while df still shows free data space; new
#      metadata chunks come only from unallocated space.
#
# Every input is optional. A missing or unreadable one is SKIPPED (reported as
# "-"), never read as zero (a false emergency) or as infinite (a false
# all-clear) — so the same code is correct on btrfs-with-quota, plain btrfs,
# ext4, LVM and tmpfs.
#
# Pure functions plus a small per-filesystem rate file; nothing runs at source
# time. Test seams: DG_MOUNTINFO, DG_SYSFS_BTRFS, DG_STATE_DIR.

DG_MOUNTINFO="${DG_MOUNTINFO:-/proc/self/mountinfo}"
DG_SYSFS_BTRFS="${DG_SYSFS_BTRFS:-/sys/fs/btrfs}"
DG_STATE_DIR="${DG_STATE_DIR:-$HOME/.genesis/watchgod}"

# Tier thresholds. Percentages are of the EFFECTIVE total (the smaller of the
# filesystem and its quota). Overridable in watchgod.conf.
DG_YELLOW_PCT="${DG_YELLOW_PCT:-15}"
DG_ORANGE_PCT="${DG_ORANGE_PCT:-8}"
DG_RED_PCT="${DG_RED_PCT:-3}"
DG_RED_MIN_MB="${DG_RED_MIN_MB:-3072}"
DG_ETA_YELLOW_MIN="${DG_ETA_YELLOW_MIN:-360}"
DG_ETA_ORANGE_MIN="${DG_ETA_ORANGE_MIN:-60}"
DG_ETA_RED_MIN="${DG_ETA_RED_MIN:-10}"
DG_META_RED_PCT="${DG_META_RED_PCT:-80}"
DG_UNALLOC_RED_MB="${DG_UNALLOC_RED_MB:-1024}"

# ── Mount lookup ─────────────────────────────────────────────
dg_mount_line() {
    # The mountinfo record for the mount holding $1, as
    # "mountpoint<TAB>fstype<TAB>source<TAB>superopts". The LAST record for the
    # mount point wins, because a later mount over the same point shadows the
    # earlier one. Mount points in mountinfo escape space/tab/newline/backslash
    # as \040 \011 \012 \134; they are compared in that escaped form.
    local mnt
    mnt="$(stat -c %m -- "$1" 2>/dev/null)" || return 1
    [[ -n "$mnt" ]] || return 1
    _dg_mnt="$mnt" awk '
        function esc(s) {
            gsub(/\\/, "\\134", s); gsub(/ /, "\\040", s)
            gsub(/\t/, "\\011", s); gsub(/\n/, "\\012", s)
            return s
        }
        BEGIN { want = esc(ENVIRON["_dg_mnt"]) }
        {
            sep = 0
            for (i = 7; i <= NF; i++) if ($i == "-") { sep = i; break }
            if (!sep || $5 != want) next
            line = $5 "\t" $(sep + 1) "\t" $(sep + 2) "\t" $(sep + 3)
        }
        END { if (line == "") exit 1; print line }
    ' "$DG_MOUNTINFO" 2>/dev/null
}

dg_btrfs_uuid() {
    # The btrfs filesystem UUID (the /sys/fs/btrfs/<uuid> directory name) for a
    # mount SOURCE. Tries the by-uuid link first, then matches the resolved
    # block device against each filesystem's devices/ list. Returns 1 rather
    # than guessing when neither identifies exactly one filesystem.
    local source="$1" base u hit=""
    [[ -d "$DG_SYSFS_BTRFS" ]] || return 1
    if [[ "$source" == /dev/disk/by-uuid/* ]]; then
        u="${source##*/}"
        [[ -d "$DG_SYSFS_BTRFS/$u/allocation" ]] && { printf '%s' "$u"; return 0; }
    fi
    base="$(basename -- "$(readlink -f -- "$source" 2>/dev/null || printf '%s' "$source")")"
    [[ -n "$base" && "$base" != "/" ]] || return 1
    for u in "$DG_SYSFS_BTRFS"/*/; do
        u="${u%/}"; u="${u##*/}"
        [[ -e "$DG_SYSFS_BTRFS/$u/devices/$base" ]] || continue
        [[ -z "$hit" ]] || return 1          # two filesystems claim it: refuse
        hit="$u"
    done
    [[ -n "$hit" ]] || return 1
    printf '%s' "$hit"
}

_dg_num() {
    # Echo the single non-negative integer in file $1, or fail.
    local v
    v="$(tr -d '[:space:]' < "$1" 2>/dev/null)" || return 1
    [[ "$v" =~ ^[0-9]+$ ]] || return 1
    printf '%s' "$v"
}

# ── Measurement ──────────────────────────────────────────────
dg_measure() {
    # One line for the filesystem holding $1:
    #   "free_mb total_mb quota unalloc_mb meta_pct fstype used_mb"
    # free/total are the EFFECTIVE figures (min over statvfs and quota);
    # quota is 1 when a btrfs quota tightened them; unalloc_mb/meta_pct are "-"
    # when not btrfs or unreadable. Returns 1 only when statvfs itself fails —
    # then nothing about this filesystem is known.
    local path="$1" sv avail blocks bsize free_b total_b
    sv="$(stat -f -c '%a %b %S' -- "$path" 2>/dev/null)" || return 1
    read -r avail blocks bsize <<< "$sv"
    [[ "$avail" =~ ^[0-9]+$ && "$blocks" =~ ^[0-9]+$ && "$bsize" =~ ^[0-9]+$ ]] || return 1
    (( blocks > 0 )) || return 1
    free_b=$(( avail * bsize ))
    total_b=$(( blocks * bsize ))

    local mline fstype="" source="" opts="" quota=0 unalloc="-" meta="-" used_b=-1
    if mline="$(dg_mount_line "$path")"; then
        IFS=$'\t' read -r _ fstype source opts <<< "$mline"
    fi
    if [[ "$fstype" == btrfs ]]; then
        local uuid
        if uuid="$(dg_btrfs_uuid "$source")"; then
            local fsdir="$DG_SYSFS_BTRFS/$uuid"
            # Quota on the subvolume that holds the path (level-0 qgroup
            # 0/<subvolid>). limit_flags bit 1 = max_referenced enforced,
            # bit 2 = max_exclusive enforced (BTRFS_QGROUP_LIMIT_MAX_RFER /
            # _MAX_EXCL). Each enforced limit is a separate wall; take both.
            #
            # The kernel checks a write against usage PLUS the qgroup's
            # outstanding reservations (rsv_data: dirty data not yet
            # committed; rsv_meta_*: metadata reserved for the open
            # transaction), so headroom subtracts them too. MEASURED on a live
            # install: rsv_meta_pertrans swung between 0.14 GB and 18.5 GB
            # minutes apart — ignoring it overstated headroom by up to 13 %.
            # A quota tree the kernel itself flags `inconsistent` is not
            # trusted at all.
            local subvolid qd flags lim cur rsv=0 r v_r
            subvolid="$(sed -n 's/.*\(^\|,\)subvolid=\([0-9][0-9]*\).*/\2/p' <<< "$opts")"
            qd="$fsdir/qgroups/0_${subvolid}"
            if [[ "$(_dg_num "$fsdir/qgroups/inconsistent" 2>/dev/null || echo 0)" == 1 ]]; then
                qd=""
            fi
            for r in rsv_data rsv_meta_pertrans rsv_meta_prealloc; do
                [[ -n "$qd" ]] && v_r="$(_dg_num "$qd/$r")" && rsv=$(( rsv + v_r ))
            done
            # Whichever wall is CLOSEST binds, and supplies all three figures
            # (free, total, used): a quota that is roomier than the pool under
            # it must not feed its own flat usage into the growth rate while
            # other subvolumes fill the pool (review finding).
            local k k_lim k_cur qfree
            if [[ -n "$subvolid" && -n "$qd" && -d "$qd" ]] && flags="$(_dg_num "$qd/limit_flags")"; then
                for k in referenced:1 exclusive:2; do
                    (( flags & ${k#*:} )) || continue
                    k_lim="$qd/max_${k%%:*}"; k_cur="$qd/${k%%:*}"
                    lim="$(_dg_num "$k_lim")" && cur="$(_dg_num "$k_cur")" && (( lim > 0 )) || continue
                    # Headroom subtracts the reservations; the growth rate
                    # does not use them (they swing by gigabytes between
                    # commits and would read as a write burst).
                    qfree=$(( lim > cur + rsv ? lim - cur - rsv : 0 ))
                    if (( qfree < free_b )); then
                        free_b=$qfree
                        total_b=$(( lim < total_b ? lim : total_b ))
                        used_b=$cur
                        quota=1
                    fi
                done
            fi
            # Unallocated = raw device bytes − raw bytes already carved into
            # chunks. disk_total is the RAW footprint (a DUP metadata profile
            # shows twice its logical size), so both sides are raw bytes and
            # multi-device / RAID profiles need no special casing here.
            local dev_b=0 alloc_b=0 ok=1 d v k
            for d in "$fsdir"/devices/*; do
                [[ -e "$d/size" ]] || { ok=0; break; }
                v="$(_dg_num "$d/size")" || { ok=0; break; }
                dev_b=$(( dev_b + v * 512 ))
            done
            for k in data metadata system; do
                v="$(_dg_num "$fsdir/allocation/$k/disk_total")" || { ok=0; break; }
                alloc_b=$(( alloc_b + v ))
            done
            if (( ok && dev_b > 0 )); then
                unalloc=$(( dev_b > alloc_b ? (dev_b - alloc_b) / 1048576 : 0 ))
            fi
            # Metadata occupancy counts what is reserved and pinned for the
            # running transaction, not just what is written; a metadata group
            # whose remaining room is inside twice the global reserve is as
            # good as full, since the global reserve exists precisely for the
            # last writes before ENOSPC. That case reports 100.
            local mu mr mp mt gr
            if mu="$(_dg_num "$fsdir/allocation/metadata/bytes_used")" \
                    && mt="$(_dg_num "$fsdir/allocation/metadata/total_bytes")" && (( mt > 0 )); then
                mr="$(_dg_num "$fsdir/allocation/metadata/bytes_reserved")" || mr=0
                mp="$(_dg_num "$fsdir/allocation/metadata/bytes_pinned")" || mp=0
                meta=$(( (mu + mr + mp) * 100 / mt ))
                if gr="$(_dg_num "$fsdir/allocation/global_rsv_size")" \
                        && (( mt - mu - mr - mp < 2 * gr )); then
                    meta=100
                fi
                (( meta > 100 )) && meta=100
            fi
        fi
    fi
    (( used_b < 0 )) && used_b=$(( total_b - free_b ))
    printf '%s %s %s %s %s %s %s\n' \
        "$(( free_b / 1048576 ))" "$(( total_b / 1048576 ))" "$quota" "$unalloc" "$meta" "${fstype:--}" \
        "$(( used_b / 1048576 ))"
}

dg_raw_total_mb() {
    # The RAW statvfs size of the filesystem holding $1, in MB (0 if unknown).
    # It identifies a limit domain: a project quota changes it, a btrfs qgroup
    # does not, and — unlike dg_measure's effective total — it never moves with
    # whichever wall currently binds.
    local b s
    read -r b s < <(stat -f -c '%b %S' -- "$1" 2>/dev/null) || { echo 0; return 0; }
    [[ "$b" =~ ^[0-9]+$ && "$s" =~ ^[0-9]+$ ]] || { echo 0; return 0; }
    echo $(( b * s / 1048576 ))
}

dg_raw_sizes_mb() {
    # "<size of $1> <size of $2>" in MB, raw statvfs, from ONE stat call so
    # both are read microseconds apart: two separate calls let a ZFS commit
    # land between them and flip a domain's key for a poll (review finding).
    # 0 for any size that cannot be read.
    local b1=0 s1=0 b2=0 s2=0
    { read -r b1 s1 && read -r b2 s2; } < <(stat -f -c '%b %S' -- "$1" "$2" 2>/dev/null) || true
    [[ "$b1" =~ ^[0-9]+$ && "$s1" =~ ^[0-9]+$ ]] || { b1=0; s1=0; }
    [[ "$b2" =~ ^[0-9]+$ && "$s2" =~ ^[0-9]+$ ]] || { b2=0; s2=0; }
    echo "$(( b1 * s1 / 1048576 )) $(( b2 * s2 / 1048576 ))"
}

dg_same_size() {
    # 0 when sizes $1 and $2 (MB, both non-zero) are within 1 % of each other.
    local a="$1" b="$2" d
    (( a > 0 && b > 0 )) || return 1
    d=$(( a > b ? a - b : b - a ))
    (( d * 100 <= (a > b ? a : b) ))
}

# ── Growth rate and time-to-full ─────────────────────────────
dg_rate_update() {
    # Update and echo the smoothed growth rate (MB/min, integer, may be
    # negative) of USED space on filesystem key $1, given current used MB $2
    # and epoch seconds $3. Exponential smoothing, alpha 0.3: a single burst
    # (a 350 MB pip unpack inside one 30 s poll) moves it only part-way, while
    # a sustained runaway converges within a few polls.
    #
    # USED, not free: a quota or filesystem grown underneath us raises free
    # space without anything being deleted, and measuring free would read that
    # as the disk "shrinking". Persisted so a restart does not reset it.
    #
    # $4 names the BINDING limit (e.g. "quota" or "fs"). Each binding keeps
    # its OWN series: the two measure different usage (qgroup vs pool), so a
    # sample from one never feeds the other. A single shared series had to be
    # reset on every change, and near a quota's crossover — where reservations
    # swing by gigabytes and the closer wall alternates poll to poll — it never
    # built a rate at all, silencing the time-to-full tiers (review finding).
    local key="$1" used="$2" now="$3" binding="${4:-fs}" f prev_t prev_u prev_r prev_b rate
    f="$DG_STATE_DIR/rate_${key}_${binding}"
    rate=0
    if read -r prev_t prev_u prev_r prev_b 2>/dev/null < "$f" \
            && [[ "$prev_t" =~ ^[0-9]+$ && "$prev_u" =~ ^[0-9]+$ && "$prev_r" =~ ^-?[0-9]+$ ]] \
            && [[ "${prev_b:-fs}" == "$binding" ]] \
            && (( now > prev_t )); then
        local dt=$(( now - prev_t )) inst
        # A gap of more than an hour (daemon stopped) says nothing about now.
        if (( dt <= 3600 )); then
            inst=$(( (used - prev_u) * 60 / dt ))
            rate=$(( (3 * inst + 7 * prev_r) / 10 ))
        fi
    fi
    mkdir -p "$DG_STATE_DIR" 2>/dev/null || true
    { printf '%s %s %s %s\n' "$now" "$used" "$rate" "$binding" > "$f.tmp" \
        && mv -f "$f.tmp" "$f"; } 2>/dev/null || true
    printf '%s' "$rate"
}

dg_eta_min() {
    # Minutes until full at rate $2 MB/min with $1 MB free; "-" when not filling.
    local free="$1" rate="$2"
    if (( rate > 0 )); then printf '%s' "$(( free / rate ))"; else printf '%s' "-"; fi
}

# ── Tiering ──────────────────────────────────────────────────
_dg_rank() { case "$1" in green) echo 0;; yellow) echo 1;; orange) echo 2;; red) echo 3;; *) echo 0;; esac; }
_dg_name() { case "$1" in 0) echo green;; 1) echo yellow;; 2) echo orange;; *) echo red;; esac; }

dg_floor_tier() {
    # Tier from free space alone ($1 free MB of $2 total MB, and btrfs
    # unallocated $3 / metadata % $4, "-" when unknown).
    local free="$1" total="$2" unalloc="${3:--}" meta="${4:--}" pct red_mb
    # total 0 = size unknown (unreadable), or a filesystem under 1 MiB, which
    # the guardian's whole-MB unit cannot measure. Neither is judged: a tier
    # needs a size. The watched filesystems (/, cc-tmp, /tmp) are all far
    # above that floor.
    (( total > 0 )) || { echo green; return; }
    pct=$(( free * 100 / total ))
    red_mb=$(( total * DG_RED_PCT / 100 ))
    (( red_mb < DG_RED_MIN_MB )) && red_mb=$DG_RED_MIN_MB
    # The absolute floor exists for big disks, where 3 % is too thin a margin.
    # It never exceeds three quarters of the ORANGE line: otherwise a small
    # filesystem (a 512 MB tmpfs /tmp, a 2 GiB cc-tmp quota) sits in RED forever,
    # and one under ~40 GiB jumps from YELLOW straight to RED with no WARNING
    # page and no standard reclaim before the last-resort one (found by review).
    # A cap just below the line would leave an ORANGE band 1 MB wide. Big disks
    # are unaffected: there 3 % is far below the line anyway.
    local orange_mb=$(( total * DG_ORANGE_PCT / 100 ))
    (( red_mb > orange_mb * 3 / 4 )) && red_mb=$(( orange_mb * 3 / 4 ))
    # Integer division rounds that cap to 0 on a filesystem under ~25 MB, and
    # `free < 0` is never true — a completely full one stayed ORANGE (review
    # finding, #2521 item 2). 1 MB is the floor.
    (( red_mb < 1 )) && red_mb=1
    if (( free < red_mb )); then echo red; return; fi
    if [[ "$unalloc" =~ ^[0-9]+$ && "$meta" =~ ^[0-9]+$ ]] \
            && (( unalloc < DG_UNALLOC_RED_MB && meta >= DG_META_RED_PCT )); then
        echo red; return
    fi
    if (( pct < DG_ORANGE_PCT )); then echo orange
    elif (( pct < DG_YELLOW_PCT )); then echo yellow
    else echo green
    fi
}

dg_eta_tier() {
    local eta="$1"
    [[ "$eta" =~ ^[0-9]+$ ]] || { echo green; return; }
    if (( eta < DG_ETA_RED_MIN )); then echo red
    elif (( eta < DG_ETA_ORANGE_MIN )); then echo orange
    elif (( eta < DG_ETA_YELLOW_MIN )); then echo yellow
    else echo green
    fi
}

dg_tier() {
    # The tier that DRIVES actions, from the floor tier $1 and the ETA tier $2.
    # Time-to-full may raise the floor tier by ONE level at most. A burst at
    # high free space (a local copy at 200 MB/s with 140 GB free projects "full
    # in 12 minutes") therefore logs attribution instead of paging and reclaiming,
    # while the same rate on a nearly full disk escalates straight to RED.
    local f e
    f="$(_dg_rank "$1")"; e="$(_dg_rank "$2")"
    (( e > f + 1 )) && e=$(( f + 1 ))
    _dg_name $(( e > f ? e : f ))
}

dg_worse() {
    local a b
    a="$(_dg_rank "$1")"; b="$(_dg_rank "$2")"
    _dg_name $(( a > b ? a : b ))
}

# ── Who is writing? ──────────────────────────────────────────
# Attribution reads /proc/<pid>/io write_bytes: bytes this process CAUSED to be
# sent to the storage layer (page-cache writeback is charged to the writer that
# dirtied the page, so a buffered writer is still found). It is blind to tmpfs
# (never reaches a block device) and to other uids (their io file is EACCES to
# us) — the page says so rather than implying the list is complete.
DG_PROC="${DG_PROC:-/proc}"

dg_io_snapshot() {
    # One line per readable own-uid process: "pid starttime write_bytes comm".
    # starttime (stat field 22) pins the IDENTITY of the pid, so a recycled pid
    # is never mistaken for the process measured a poll earlier. comm can hold
    # spaces and parentheses, so stat is parsed after its LAST ')'.
    #
    # Builtins only — no fork per process. MEASURED: forking awk/cat/tr for each
    # of ~170 processes cost ~2.8 s of CPU per poll, which a 5 s fast poll
    # cannot afford.
    local p pid k v wb st comm rest
    for p in "$DG_PROC"/[0-9]*; do
        [[ -O "$p" ]] || continue
        pid="${p##*/}"
        wb=""
        while read -r k v; do
            [[ "$k" == write_bytes: ]] && { wb="$v"; break; }
        done 2>/dev/null < "$p/io" || continue
        [[ "$wb" =~ ^[0-9]+$ ]] || continue
        rest=""
        read -r rest 2>/dev/null < "$p/stat" || [[ -n "$rest" ]] || continue
        rest="${rest##*) }"
        # rest starts at field 3 (state); starttime is field 22 → word 20 here.
        read -r -a _dg_f <<< "$rest"
        st="${_dg_f[19]:-}"
        [[ "$st" =~ ^[0-9]+$ ]] || continue
        comm="?"
        read -r comm 2>/dev/null < "$p/comm" || true
        # comm is process-chosen (prctl PR_SET_NAME) and may hold tabs or
        # newlines; those would split the space-separated snapshot, so every
        # whitespace byte becomes "_".
        comm="${comm//[[:space:]]/_}"
        printf '%s %s %s %s\n' "$pid" "$st" "$wb" "$comm"
    done
    return 0
}

dg_io_top() {
    # Given the previous and current snapshots ($1, $2) and the seconds between
    # them ($3), print the top N ($4, default 5) writers by bytes written in the
    # interval: "pid starttime delta_bytes rate_mb_per_min comm", largest first.
    # A pid whose starttime changed is a DIFFERENT process and has no delta.
    local prev="$1" cur="$2" dt="${3:-30}" n="${4:-5}"
    (( dt > 0 )) || dt=30
    awk -v dt="$dt" '
        NR == FNR { if (NF >= 3) base[$1 " " $2] = $3; next }
        ($1 " " $2) in base {
            d = $3 - base[$1 " " $2]
            if (d > 0) printf "%s %s %d %d %s\n", $1, $2, d, d * 60 / dt / 1048576, $4
        }
    ' <(printf '%s\n' "$prev") <(printf '%s\n' "$cur") | sort -k3,3nr | awk -v n="$n" 'NR <= n'
}
