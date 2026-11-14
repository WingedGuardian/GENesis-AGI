#!/usr/bin/env bash
# Genesis backup — wait for the round-trip verify to fit in memory.
#
# Runs as genesis-backup.service's ExecStartPre=-, before backup.sh. backup.sh
# proves every SQLite artifact restores by rebuilding the whole database from
# it: in memory when there is room (_pick_verify_mode), on disk otherwise. On a
# shared or spinning disk the on-disk rebuild is a burst of writes that can stall
# every other machine on the pool, so when memory is short this waits, up to a
# cap, for it to free.
#
# The cap is GENESIS_BACKUP_VERIFY_WAIT_MAX_S, set in the UNIT: it sits beside
# TimeoutStartSec there, so the wait can never outgrow the timeout that bounds
# it. secrets.env may LOWER it (0 turns the wait off) but never raise it.
# Anywhere but the unit there is no cap, so nothing waits: update.sh's own
# pre-update backup, tests, CI and uninstall all run backup.sh directly.
#
# It holds no lock and writes nothing, so a restore can run meanwhile and a kill
# mid-wait leaves no trace (as backup.sh's lock-skip leaves none). It always
# exits 0: what it decides only delays the backup, never stops it.
#
# Knobs (secrets.env or environment, except the cap):
#   GENESIS_BACKUP_VERIFY_WAIT_INTERVAL_S  re-check interval, default 600
#   GENESIS_BACKUP_VERIFY_WAIT_MARGIN_PCT  headroom above the line, % of MemTotal,
#                                          default 5: backup.sh decides minutes
#                                          later (after its integrity check), and
#                                          memory moves meanwhile
#   GENESIS_BACKUP_VERIFY_MEMORY=off       backup.sh always verifies on disk: no wait
#   GENESIS_BACKUP_VERIFY_RAM_FACTOR / _FLOOR_PCT  the predicate's own knobs

# No `set -e`: an unexpected failure must end the wait, loudly, and let the
# backup run, never abort with a status systemd may not surface.
_LOG_PREFIX="[genesis-backup]"
_say() { echo "$_LOG_PREFIX $(date -Iseconds) $*"; }
_give_up() { _say "verify memory wait: $*; continuing without the wait"; exit 0; }
trap '_give_up "failed at line $LINENO"' ERR

# The cap comes from the unit before secrets.env can export over it.
_cap_raw="${GENESIS_BACKUP_VERIFY_WAIT_MAX_S:-}"

if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || _give_up "HOME is unset"
    export HOME
fi
_SCRIPT_DIR="$(unset CDPATH; cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || _give_up "cannot resolve the script directory"
GENESIS_DIR="${GENESIS_DIR:-$HOME/genesis}"

for _lib in live_system_guard.sh secrets_path.sh load_secrets.sh backup_verify_memory.sh; do
    # shellcheck source=/dev/null
    source "$_SCRIPT_DIR/lib/$_lib" 2>/dev/null || _give_up "cannot load lib/$_lib"
done
refuse_if_test_fenced_live "$GENESIS_DIR" || exit 0

_cap=$(_num "$_cap_raw" 0)
[ "$_cap" -gt 0 ] || exit 0

load_secrets_file "$(genesis_secrets_path "$GENESIS_DIR")"
# secrets.env may re-point GENESIS_DIR (backup.sh then verifies that tree's DB,
# so this measures the same one): fence the value actually used, too.
refuse_if_test_fenced_live "$GENESIS_DIR" || exit 0
# A secrets.env value may only lower the unit's cap; an invalid one changes nothing.
_cap_secrets=$(_num "${GENESIS_BACKUP_VERIFY_WAIT_MAX_S:-}" "$_cap")
[ "$_cap_secrets" -ge "$_cap" ] || _cap=$_cap_secrets
[ "$_cap" -gt 0 ] || exit 0
if [ "${GENESIS_BACKUP_VERIFY_MEMORY:-on}" = off ]; then
    _say "verify memory wait: GENESIS_BACKUP_VERIFY_MEMORY=off, backup.sh verifies on disk; no wait"
    exit 0
fi
# Derived after the secrets load, as backup.sh derives it.
_db="$GENESIS_DIR/data/genesis.db"
[ -f "$_db" ] || { _say "verify memory wait: no database at $_db; no wait"; exit 0; }

_interval=$(_num "${GENESIS_BACKUP_VERIFY_WAIT_INTERVAL_S:-}" 600)
[ "$_interval" -ge 1 ] || _interval=600
_margin_pct=$(_num "${GENESIS_BACKUP_VERIFY_WAIT_MARGIN_PCT:-}" 5)

# Time waited is counted from the sleeps themselves: a check costs well under
# a second, and counting what was slept keeps the cap exact.
_waited=0
while :; do
    _need_kb=0 _floor_kb=0 _avail_kb=0 _total_kb=0
    _verify_memory_check "$_db" || true
    _margin_kb=$(( ${_total_kb:-0} * _margin_pct / 100 ))
    _line_kb=$(( _need_kb + _floor_kb + _margin_kb ))
    # backup.sh's own line has no margin; say whether it is met, so a log can
    # tell "waited for nothing" from "the wait mattered".
    if [ "${_avail_kb:-0}" -ge $(( _need_kb + _floor_kb )) ]; then _met=yes; else _met=no; fi
    _numbers="need ${_need_kb} KB + floor ${_floor_kb} KB + margin ${_margin_kb} KB, available ${_avail_kb:-0} KB; backup.sh's line met: ${_met}"
    # A line above all of memory can never be met: waiting would only delay.
    if [ "$_line_kb" -gt "${_total_kb:-0}" ]; then
        _say "verify memory can never fit ($_numbers; MemTotal ${_total_kb:-0} KB); no wait"
        exit 0
    fi
    if [ "${_avail_kb:-0}" -ge "$_line_kb" ]; then
        if [ "$_waited" -gt 0 ]; then
            _say "verify memory fits after ${_waited}s ($_numbers)"
        else
            _say "verify memory fits ($_numbers)"
        fi
        exit 0
    fi
    _left=$(( _cap - _waited ))
    if [ "$_left" -le 0 ]; then
        _say "verify memory still short after ${_waited}s ($_numbers); backup.sh decides again and may verify on disk"
        exit 0
    fi
    _step=$_interval
    [ "$_step" -le "$_left" ] || _step=$_left
    _say "waiting for verify memory ($_numbers); next check in ${_step}s, ${_left}s left"
    sleep "$_step" || true
    _waited=$(( _waited + _step ))
done
