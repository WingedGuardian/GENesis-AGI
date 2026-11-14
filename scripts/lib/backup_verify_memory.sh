# shellcheck shell=bash
# Genesis — does the backup's round-trip verify fit in memory? Sourced, not executed.
#
# One predicate, two callers: backup.sh's _pick_verify_mode, which chooses where
# the verify rebuilds the database, and scripts/backup_wait_for_ram.sh, the
# backup unit's ExecStartPre that waits until it fits. Keeping one copy means
# the wait cannot drift from the choice it is waiting to change.
#
# Defines _num, _cgroup_headroom_kb and _verify_memory_check. Reads GENESIS_BACKUP_CGROUP_FS / GENESIS_BACKUP_PROC_CGROUP (test overrides)
# at call time, so a value loaded from secrets.env after sourcing still counts.

# _num <value> <default> — a decimal integer of at most 6 digits, else the
# default. 10# stops "08" aborting the run as a bad octal under set -e, and the
# length cap keeps the arithmetic below from overflowing.
_num() {
    case "$1" in ''|*[!0-9]*) echo "$2" ;; *) [ "${#1}" -le 6 ] && echo $((10#$1)) || echo "$2" ;; esac
}

# _cgroup_headroom_kb — the smallest (limit - usage + inactive_file) over this
# process's cgroup and its ancestors, in KB; empty when no level is capped.
# Usage includes page cache, which fills toward the cap on any long-running
# box, so the cold part of it (inactive_file) counts as available, the way
# MemAvailable counts it. /proc/meminfo reports the host's memory under a capped
# Docker/systemd cgroup without lxcfs, so it alone could approve memory a cap
# does not have. A v1 memory-controller line wins over "0::": a hybrid host has
# both, and its cap lives on the v1 side. Each level is read on its own (its own
# limit against its own usage), so a cap on an ancestor counts everything under
# it. A level with a cap but unreadable usage counts as no headroom.
_cgroup_headroom_kb() {
    local _p _d _root _m _c _i _h _best="" _v1=false
    local _cgroup_fs="${GENESIS_BACKUP_CGROUP_FS:-/sys/fs/cgroup}"
    local _proc_cgroup="${GENESIS_BACKUP_PROC_CGROUP:-/proc/self/cgroup}"
    _p=$(awk -F: '$2 ~ /(^|,)memory(,|$)/ {print $3; exit}' "$_proc_cgroup" 2>/dev/null) || _p=""
    if [ -n "$_p" ]; then
        _v1=true
        _root="$_cgroup_fs/memory"
    else
        _p=$(awk -F: '$1 == "0" {print $3; exit}' "$_proc_cgroup" 2>/dev/null) || _p=""
        [ -n "$_p" ] || return 0
        _root="$_cgroup_fs"
    fi
    _d="$_root${_p%/}"
    while :; do
        if $_v1; then
            _m=$(cat "$_d/memory.limit_in_bytes" 2>/dev/null) || _m=""
            _c=$(cat "$_d/memory.usage_in_bytes" 2>/dev/null) || _c=""
            _i=$(awk '$1 == "total_inactive_file" {print $2}' "$_d/memory.stat" 2>/dev/null) || _i=""
        else
            _m=$(cat "$_d/memory.max" 2>/dev/null) || _m=""
            _c=$(cat "$_d/memory.current" 2>/dev/null) || _c=""
            _i=$(awk '$1 == "inactive_file" {print $2}' "$_d/memory.stat" 2>/dev/null) || _i=""
        fi
        case "$_i" in ''|*[!0-9]*) _i=0 ;; esac
        # A cap is a decimal below 2^60: v2 writes "max" for none, and v1 a
        # value near 2^63 (19 digits), which is also kept out of the arithmetic.
        case "$_m" in ''|*[!0-9]*) _m="" ;; esac
        if [ -n "$_m" ] && [ "${#_m}" -le 18 ] && [ "$_m" -lt 1152921504606846976 ]; then
            case "$_c" in
                ''|*[!0-9]*) _h=0 ;;
                *) [ "${#_c}" -le 18 ] && _h=$(( (_m - _c + _i) / 1024 )) || _h=0 ;;
            esac
            [ "$_h" -ge 0 ] || _h=0
            if [ -z "$_best" ] || [ "$_h" -lt "$_best" ]; then _best=$_h; fi
        fi
        [ "$_d" = "$_root" ] && break
        _d="${_d%/*}"
        case "$_d" in "$_root"*) ;; *) break ;; esac
    done
    echo "$_best"
}

# _verify_memory_check <db_file> — return 0 when the in-memory verify fits.
# It needs available memory of VERIFY_RAM_FACTOR x the live DB size (the
# rebuilt DB plus sqlite's working set) PLUS a floor of VERIFY_RAM_FLOOR_PCT of
# MemTotal. Available is the lower of MemAvailable and the cgroup headroom. The
# floor is there because systemd-oomd kills sessions on sustained reclaim
# pressure. Sets the caller-visible _need_kb, _floor_kb, _avail_kb and
# _total_kb (not local, so a caller's own locals of those names receive them).
_verify_memory_check() {
    local _db="$1" _factor _floor_pct _db_bytes _wal_bytes _cg
    _factor=$(_num "${GENESIS_BACKUP_VERIFY_RAM_FACTOR:-}" 2)
    [ "$_factor" -ge 2 ] || _factor=2  # the rebuilt DB plus its index sorts
    _floor_pct=$(_num "${GENESIS_BACKUP_VERIFY_RAM_FLOOR_PCT:-}" 20)
    _db_bytes=$(stat -c %s "$_db" 2>/dev/null) || _db_bytes=0
    # Committed data still in the WAL is part of the dump, and so of the
    # rebuilt DB, but not of the main file's size.
    _wal_bytes=$(stat -c %s "$_db-wal" 2>/dev/null) || _wal_bytes=0
    _db_bytes=$(( _db_bytes + _wal_bytes ))
    _need_kb=$(( (_db_bytes / 1024 + 1) * _factor ))
    _avail_kb=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo 2>/dev/null) || _avail_kb=0
    _total_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null) || _total_kb=0
    _cg=$(_cgroup_headroom_kb) || _cg=""
    if [ -n "$_cg" ] && [ "$_cg" -lt "${_avail_kb:-0}" ]; then _avail_kb=$_cg; fi
    _floor_kb=$(( ${_total_kb:-0} * _floor_pct / 100 ))
    [ "${_avail_kb:-0}" -ge $(( _need_kb + _floor_kb )) ]
}
