#!/usr/bin/env bash
# tmp_watchgod.sh — whole-disk guardian (v2), plus cgroup OOM event capture.
#
# Runs as a standalone systemd user service, independent of Genesis: it is the
# layer that must still work when genesis-server (and every alarm inside it) is
# the thing that has died.
#
# WHAT IT GUARDS. Every distinct filesystem Genesis writes to — the root disk,
# $HOME, /tmp, the cc-tmp volume, ~/tmp/downloads — measured by the tightest of
# statvfs, the btrfs quota on the subvolume, and btrfs metadata/unallocated
# space (scripts/lib/disk_guardian.sh explains each). A full disk is the one
# failure that stops every session and service at once; nothing here exists to
# keep any single directory under an arbitrary size.
#
# RETENTION. Hourly, it sweeps cc-tmp — Claude Code's working temp — of what
# ENDED sessions left behind: a unit nothing alive holds and nothing has
# touched for 7 days (sweep_cc_tmp). That is the only thing it deletes.
#
# WHAT IT DOES, by tier (per filesystem; free space AND time-to-full, see
# dg_tier). It never deletes working files and never kills a process:
#   YELLOW  log attribution once per episode: who is writing, and where the
#           space went.
#   ORANGE  reclaim: on the $HOME filesystem start genesis-disk-hygiene-
#           pressure@standard (caches, ~/tmp older than 2 days, live writers
#           spared); on cc-tmp's, run the retention sweep at 2 days. Page a
#           WARNING.
#   RED     release the reserve file, start genesis-disk-hygiene-pressure@
#           last-resort, page EMERGENCY with attribution.
# Reclaim levers only run for the filesystem they can actually relieve; the
# others page with attribution.
#
# v1 of this daemon enforced a 500 MB budget on cc-tmp and swept /tmp by age,
# deleting live work to stay under numbers nobody had justified. Both are gone.
#
# Reads ~/.genesis/config/watchgod.conf, then watchgod.local.conf (install-local
# overrides that the conf generator never rewrites).
# Writes ~/.genesis/watchgod_state.json; logs to ~/.genesis/logs/tmp_watchgod.log.

set -euo pipefail

# Resolve HOME when unset: stripped-env/systemd/sandbox invocations can leave
# HOME unset, which under `set -u` aborts at the first ${HOME} use. Fall back
# to the passwd entry for the current uid (same source Path.home() uses); fail
# closed if unresolvable. See CC memory sandbox_shell_no_home.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "ERROR: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

POLL_INTERVAL=30
CONF_FILE="$HOME/.genesis/config/watchgod.conf"
STATE_FILE="$HOME/.genesis/watchgod_state.json"
LOG_FILE="$HOME/.genesis/logs/tmp_watchgod.log"
ALERT_DIR="$HOME/.genesis/alerts"

# OOM event capture: the container cgroup-v2 cumulative oom_kill counter, and a
# durable log for the snapshots. OOM_EVENTS_FILE is overridable so tests can
# point it at a fixture file. OOM_LOG lives beside the watchgod log (NOT in
# cc-tmp) so it survives cleanup.
OOM_EVENTS_FILE="${OOM_EVENTS_FILE:-/sys/fs/cgroup/memory.events}"
# The trigger discriminator (Codex P1, #1790): a unit's `oom_kill` count records
# WHICH process died, not WHOSE limit fired — when the container (or any
# ancestor) hits its limit, the kernel can pick a high-RSS victim inside a
# contained child scope, and the journal then names that child. Only the cgroup
# whose OWN limit was hit records a LOCAL `oom` event, so the container root's
# memory.events.local `oom` counter distinguishes container pressure from a
# contained cap doing its job. Readable since kernel 4.19 wherever
# memory.events is; unreadable here means the trigger cannot be verified and
# the kill PAGES (attribution never silences on missing evidence).
# Deliberately NOT resolved here. An explicit override wins, but the DEFAULT is
# derived from OOM_EVENTS_FILE at the moment it is read (see
# _read_oom_local_trigger), because resolving it at source time freezes it
# against whatever OOM_EVENTS_FILE happened to be then — and anything that
# reassigns OOM_EVENTS_FILE afterwards leaves this pointing at the real
# /sys/fs/cgroup while believing otherwise. That is silent, and it is
# environment-dependent: it reads correct on a host that has the file and
# inverts every suppression decision on one that does not.
OOM_EVENTS_LOCAL_FILE="${OOM_EVENTS_LOCAL_FILE:-}"
# shellcheck disable=SC2034  # read by scripts/lib/watchgod_oom.sh
OOM_LOG="$(dirname "$LOG_FILE")/oom_events.log"

# Durable alert queue (F.3) — emergency-tier events page Telegram via the
# container drainer. Guarded: if the lib is ever not co-located, degrade to a
# no-op so `set -e` can never take the service down over an alert.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$_SCRIPT_DIR/lib/alert_queue.sh" ]]; then
    # shellcheck source=scripts/lib/alert_queue.sh
    source "$_SCRIPT_DIR/lib/alert_queue.sh"
else
    queue_alert() { :; }
    queue_alert_try() { return 1; }
fi

# Whole-disk guardian: measurement + tiering + attribution primitives.
# shellcheck source=scripts/lib/disk_guardian.sh
source "$_SCRIPT_DIR/lib/disk_guardian.sh"
# Liveness (open fds + cwds) for the cc-tmp retention sweep.
# shellcheck source=scripts/lib/tmp_liveness.sh
source "$_SCRIPT_DIR/lib/tmp_liveness.sh"

LOCAL_CONF_FILE="$HOME/.genesis/config/watchgod.local.conf"
FAST_POLL_INTERVAL=5

# Defaults (overridden by config)
CC_TMP_DIR="$HOME/.genesis/cc-tmp"
DOWNLOADS_DIR="$HOME/tmp/downloads"
# 1 = act; 0 = OBSERVE: log what each tier WOULD do, change nothing, page nothing.
WATCHGOD_ACT=1
# Extra paths whose filesystems should be watched, space-separated.
WATCH_EXTRA_PATHS=""
# Reserve file: preallocated space released at RED so the last writes (logs,
# DB commits, the reclaim itself) have room. min(this, 1 % of the filesystem).
RESERVE_MAX_MB=2048
# Re-start the reclaim unit at most this often while a tier persists.
PRESSURE_RETRIGGER_S=600
# Where space usually goes on this layout; `du` of each is logged at YELLOW.
DG_ATTRIBUTION_PATHS=""
OOM_CONTAINED_UNIT_PREFIXES="${OOM_CONTAINED_UNIT_PREFIXES:-code-intel- cbm-mcp-}"

# ── Load config ──────────────────────────────────────────────
# Every tunable a conf file may set. Their STARTUP values (code default, or an
# environment override) are snapshotted once, and load_config restores them
# before each re-read — otherwise deleting a line from watchgod.conf would keep
# its old value until the daemon restarted, because sourcing a file only ever
# SETS variables. MEASURED in the E2E: a forced-RED threshold outlived its
# removal and pinned the disk at RED.
_WG_TUNABLES="CC_TMP_DIR DOWNLOADS_DIR WATCHGOD_ACT WATCH_EXTRA_PATHS RESERVE_MAX_MB
    POLL_INTERVAL FAST_POLL_INTERVAL
    PRESSURE_RETRIGGER_S DG_ATTRIBUTION_PATHS
    DG_YELLOW_PCT DG_ORANGE_PCT DG_RED_PCT DG_RED_MIN_MB
    DG_ETA_YELLOW_MIN DG_ETA_ORANGE_MIN DG_ETA_RED_MIN DG_META_RED_PCT DG_UNALLOC_RED_MB
    CC_SWEEP_INTERVAL_S CC_SWEEP_AGE_MIN CC_SWEEP_PRESSURE_AGE_MIN OOM_CONTAINED_UNIT_PREFIXES"
declare -A _WG_BASE=()
_wg_snapshot_defaults() {
    local k
    for k in $_WG_TUNABLES; do _WG_BASE[$k]="${!k-}"; done
}

load_config() {
    local f k
    if (( ${#_WG_BASE[@]} )); then
        for k in $_WG_TUNABLES; do printf -v "$k" '%s' "${_WG_BASE[$k]-}"; done
    fi
    for f in "$CONF_FILE" "$LOCAL_CONF_FILE"; do
        if [[ -f "$f" ]]; then
            # shellcheck source=/dev/null
            source "$f"
        fi
    done
    CC_TMP_DIR="$(_wg_canon "$CC_TMP_DIR")"
    DOWNLOADS_DIR="$(_wg_canon "$DOWNLOADS_DIR")"
    # The lever degrades toward LESS authority: anything but 0 or 1 observes.
    if [[ "$WATCHGOD_ACT" != 0 && "$WATCHGOD_ACT" != 1 ]]; then
        _wg_warn_once act "watchgod.conf: WATCHGOD_ACT='${WATCHGOD_ACT}' is not 0 or 1 — running in OBSERVE mode"
        WATCHGOD_ACT=0
    fi
    # Every numeric setting reaches shell arithmetic, where a hand-edited "10%"
    # or "abc" aborts under set -u and a leading zero is OCTAL: "08" is a fatal
    # "value too great for base" and "0600" silently means 384 (review
    # finding). Each one is normalised to canonical base 10 here, once, or
    # reset to its default — loudly. The last field is a floor: a retention age
    # under a day would reap an idle session's temp while its owner is at
    # lunch, so it is refused rather than obeyed.
    local kv k d min
    for kv in DG_YELLOW_PCT:15:0 DG_ORANGE_PCT:8:0 DG_RED_PCT:3:0 DG_RED_MIN_MB:3072:0 \
              DG_ETA_YELLOW_MIN:360:0 DG_ETA_ORANGE_MIN:60:0 DG_ETA_RED_MIN:10:0 \
              DG_META_RED_PCT:80:0 DG_UNALLOC_RED_MB:1024:0 RESERVE_MAX_MB:2048:0 \
              PRESSURE_RETRIGGER_S:600:0 CC_SWEEP_INTERVAL_S:3600:0 \
              CC_SWEEP_AGE_MIN:10080:1440 CC_SWEEP_PRESSURE_AGE_MIN:2880:1440 \
              POLL_INTERVAL:30:1 FAST_POLL_INTERVAL:5:1; do
        IFS=: read -r k d min <<< "$kv"
        _wg_uint "$k" "$d" "$min"
    done
}

_wg_uint() {
    # Normalise variable $1 to a canonical base-10 integer >= $3, else set $2.
    local k="$1" d="$2" min="$3" v="${!1:-}"
    if [[ "$v" =~ ^[0-9]{1,12}$ ]] && (( 10#$v >= min )); then
        printf -v "$k" '%s' "$(( 10#$v ))"
    else
        _wg_warn_once "bad_$k" "watchgod.conf: ${k}='${v}' is not a whole number >= ${min} — using ${d}"
        printf -v "$k" '%s' "$d"
    fi
}

_wg_canon() {
    local d="$1"
    while [[ "$d" == */ && "$d" != "/" ]]; do d="${d%/}"; done
    printf '%s' "$d"
}

# ── Logging ──────────────────────────────────────────────────
# Every write to the state/log filesystem is best-effort. That filesystem is
# usually the one this daemon guards, so at RED an append can fail (ENOSPC,
# EDQUOT, read-only) — and under `set -e` an unguarded one would take the
# guardian down at exactly the moment it exists for (review finding; the
# same holds for every write below).
log() {
    local level="$1"; shift
    { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [$level] $*" >> "$LOG_FILE"; } 2>/dev/null || true
}

declare -A _WG_WARNED=()
_wg_warn_once() {
    [[ -n "${_WG_WARNED[$1]:-}" ]] && return 0
    _WG_WARNED[$1]=1
    log WARN "$2"
}

_wg_json_str() {
    # A JSON string literal for $1 (backslash, quote and control characters).
    local s="$1"
    s="${s//\\/\\\\}"; s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"; s="${s//$'\t'/\\t}"; s="${s//$'\r'/\\r}"
    # Any other C0 control byte is invalid inside a JSON string; replace it.
    s="${s//[$'\x01'-$'\x08'$'\x0b'$'\x0c'$'\x0e'-$'\x1f']/?}"
    printf '"%s"' "$s"
}

# ── Which filesystems ────────────────────────────────────────
watched_paths() {
    # One path per line; the caller deduplicates by device. A path that does
    # not exist yet (no downloads dir on this install) is simply skipped.
    local p
    for p in / "$HOME" /tmp "$CC_TMP_DIR" "$DOWNLOADS_DIR" $WATCH_EXTRA_PATHS; do
        [[ -e "$p" ]] && printf '%s\n' "$p"
    done
    return 0
}

home_dev() { stat -c %d -- "$HOME" 2>/dev/null || echo -; }

# ── Episode bookkeeping ──────────────────────────────────────
# One episode per filesystem: from leaving GREEN to returning to it. Pages and
# attribution fire once per (filesystem, tier) per episode; the reserve and the
# reclaim re-evaluate on every RED poll.
_ep_file() { printf '%s/episode_%s_%s' "$DG_STATE_DIR" "$1" "$2"; }
episode_seen() { [[ -f "$(_ep_file "$1" "$2")" ]]; }
episode_mark() { { mkdir -p "$DG_STATE_DIR" && : > "$(_ep_file "$1" "$2")"; } 2>/dev/null || true; }
episode_clear() { rm -f "$DG_STATE_DIR/episode_${1}_"* 2>/dev/null || true; }

# ── Attribution ──────────────────────────────────────────────
attribution_paths() {
    local p
    if [[ -n "$DG_ATTRIBUTION_PATHS" ]]; then
        for p in $DG_ATTRIBUTION_PATHS; do printf '%s\n' "$p"; done
        return 0
    fi
    for p in "$HOME/tmp" "$DOWNLOADS_DIR" "$CC_TMP_DIR" "$HOME/genesis/.claude/worktrees" \
             "$HOME/genesis/data" "$HOME/.genesis" "$HOME/.cache" "$HOME/.npm" \
             "$HOME/.local/share" "$HOME/.claude"; do
        [[ -e "$p" ]] && printf '%s\n' "$p"
    done
    return 0
}

log_attribution() {
    # $1 path, $2 device, $3 tier, $4 top-writers text. The writer list is
    # computed by the caller (cheap); the du is backgrounded, niced and bounded,
    # because du across a large tree inside the poll loop would stop the very
    # measurements that matter while the disk fills.
    local path="$1" dev="$2" tier="$3" writers="$4"
    log WARN "disk ${tier^^} [${path}] top writers by bytes written since the last poll (own uid only; blind to tmpfs):"
    if [[ -n "$writers" ]]; then
        while IFS= read -r line; do log WARN "  writer: $line"; done <<< "$writers"
    else
        log WARN "  writer: (none measurable)"
    fi
    # Detached from the daemon's stdio: it logs through log() only, and must
    # never hold a pipe a caller is waiting on.
    (
        local -a targets=()
        local t
        if [[ "$dev" == "${HOME_KEY:-$(home_dev)}" ]]; then
            while IFS= read -r t; do
                [[ "$(stat -c %d -- "$t" 2>/dev/null)" == "${dev%%[qm]*}" ]] && targets+=("$t")
            done < <(attribution_paths)
        else
            # The WATCHED path's children, not its mount's: an extra watched
            # path can sit on a mount (even /) far larger than it.
            while IFS= read -r -d '' t; do targets+=("$t"); done \
                < <(find "$path" -mindepth 1 -maxdepth 1 -xdev -print0 2>/dev/null)
        fi
        (( ${#targets[@]} )) || exit 0
        timeout 120 nice -n 19 ionice -c3 du -smx -- "${targets[@]}" 2>/dev/null \
            | sort -rn | head -10 | while IFS= read -r line; do
                log WARN "  du [${path}]: $line"
            done
    ) </dev/null >/dev/null 2>&1 &
}

# ── Reserve file ─────────────────────────────────────────────
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
    rm -f -- "$RESERVE_FILE" && echo "released ${sz} MB (a snapshot may still pin its blocks; the next poll measures what was actually freed)"
    return 0
}

# ── Action stamps and pages ──────────────────────────────────
# Two rules every stamp, marker and page in this daemon follows (review
# findings, two rounds): an ACT-mode record is written only after the action
# SUCCEEDED — a stamp written before a failed start, or a page marked sent
# before a failed enqueue, silences the retry for a whole interval or episode
# — and an OBSERVE-mode poll keeps its own records (_wg_mode_tag), so flipping
# WATCHGOD_ACT 0→1 mid-episode acts at once instead of inheriting "already done".
_wg_mode_tag() { (( WATCHGOD_ACT )) && return 0; printf '_observe'; }

_wg_due() {
    # 0 if the stamp named $1 (mode-scoped) is at least $2 seconds old.
    local f last=0
    f="$DG_STATE_DIR/${1}$(_wg_mode_tag)"
    [[ -f "$f" ]] && last="$(cat "$f" 2>/dev/null || echo 0)"
    [[ "$last" =~ ^[0-9]{1,12}$ ]] || last=0
    local now
    now="$(date +%s)"
    # A stamp from the future (the clock stepped back) counts as due.
    (( 10#$last > now || now - 10#$last >= $2 ))
}

_wg_stamp() {
    { mkdir -p "$DG_STATE_DIR" && date +%s > "$DG_STATE_DIR/${1}$(_wg_mode_tag)"; } 2>/dev/null || true
}

_wg_page() {
    # $1 severity $2 title $3 body $4 dedupe key $5 observe-mode summary.
    # 0 only when the page was queued (or, observing, logged): the caller marks
    # the episode on 0, so a failed enqueue is retried on the next poll.
    if (( WATCHGOD_ACT == 0 )); then
        log WARN "OBSERVE: would page ${5}"
        return 0
    fi
    if queue_alert_try "$1" "watchgod:disk" "$2" "$3" "$4"; then
        return 0
    fi
    log WARN "could not queue the page '${2}' (alert queue unwritable?) — retrying next poll"
    return 1
}

# ── Reclaim (the one deleter lives in disk_hygiene.sh) ───────
start_pressure_unit() {
    # $1 = standard | last-resort. Rate-limited per instance while a tier
    # persists; a repeat start of a running instance is a systemd no-op.
    local inst="$1" unit
    unit="genesis-disk-hygiene-pressure@${inst}.service"
    _wg_due "pressure_${inst}" "$PRESSURE_RETRIGGER_S" || return 0
    if (( WATCHGOD_ACT == 0 )); then
        log WARN "OBSERVE: would start ${unit}"
        _wg_stamp "pressure_${inst}"
        return 0
    fi
    if systemctl --user start --no-block "$unit" 2>/dev/null; then
        _wg_stamp "pressure_${inst}"
        log WARN "started ${unit}"
    else
        log WARN "could not start ${unit} — retrying next poll (is it rendered? bootstrap renders scripts/systemd/*.template)"
    fi
    return 0
}

# ── cc-tmp retention sweep ───────────────────────────────────
# cc-tmp is Claude Code's working temp: every session writes there, nothing
# else removes what an ENDED session leaves behind, and the volume has a hard
# quota. This sweep is its retention — the file-side counterpart of the session
# reapers, which clean up database rows and never free a byte.
#
# It is NOT v1's budget sweep. v1 deleted to stay under a number and took live
# work with it. This reaps a unit only when BOTH hold:
#   * nothing alive is using it — no open descriptor or working directory
#     inside it (tmp_liveness.sh), and it holds no socket; and
#   * nothing in it has been modified for the retention age (7 days on the
#     hourly pass, 2 days while cc-tmp's own filesystem is ORANGE/RED).
# Units: a loose top-level file; a top-level directory; and a SESSION directory
# claude-<uid>/<project>/<session> (the container and project levels are never
# units — one live session must not pin its siblings, and a sibling's age must
# not take a live session). The control plane (cc-socks*, cc-daemon-*) is
# never a unit. If the liveness snapshot comes back empty the sweep does not
# run: deleting without being able to see writers is the failure this avoids.
CC_SWEEP_INTERVAL_S=3600
CC_SWEEP_AGE_MIN=10080
CC_SWEEP_PRESSURE_AGE_MIN=2880

# Unit key of a path RELATIVE to cc-tmp (one per line on stdin): the first
# component, or claude-<uid>/<project>/<session> inside a container. Container
# and project levels map to nothing — they are never units.
_CC_UNIT_KEY_AWK='
    { n = split($0, c, "/")
      if (c[1] ~ /^claude-[0-9]+$/) { if (n >= 3) print c[1] "/" c[2] "/" c[3] }
      else if (c[1] != "") print c[1] }'

_cc_sweep_units() {
    # NUL-delimited candidate units under $1 (see the header for the model).
    local root="$1" c name uid
    uid="$(id -u 2>/dev/null || echo 0)"
    while IFS= read -r -d '' c; do
        name="${c##*/}"
        case "$name" in
            cc-socks|"cc-socks-$uid"|"cc-daemon-$uid") continue ;;
        esac
        if [[ "$name" =~ ^claude-[0-9]+$ && -d "$c" ]]; then
            find "$c" -mindepth 2 -maxdepth 2 -print0 2>/dev/null
            continue
        fi
        printf '%s\0' "$c"
    done < <(find "$root" -mindepth 1 -maxdepth 1 -print0 2>/dev/null)
    return 0
}

sweep_cc_tmp() {
    # $1 retention age in minutes, $2 why ("hourly" / "pressure").
    #
    # One find pass and three lookup tables, not a find per unit: MEASURED on a
    # live cc-tmp of ~1,000 units, the per-unit form cost 30 s of CPU and held
    # the poll loop for all of it.
    local age="$1" why="${2:-hourly}" root snap u key n=0 kept=0
    # CANONICAL root: /proc reports fully resolved paths, so a symlinked
    # ancestor (e.g. /home -> /var/home) would make every held path miss the
    # prefix and silently empty the held table.
    root="$(cd -P -- "$CC_TMP_DIR" 2>/dev/null && pwd -P)" || return 0
    if ! liveness_visible; then
        log WARN "cc-tmp sweep skipped: no process outside this daemon is visible in /proc, so nothing can be proven unused"
        return 0
    fi
    snap="$(live_open_paths)"
    local -A held=() recent=() socket=()
    # Held: every open path or cwd under cc-tmp, mapped to its unit.
    while IFS= read -r key; do [[ -n "$key" ]] && held[$key]=1; done < <(
        printf '%s\n' "$snap" | _wg_root="$root/" awk '
            / \(deleted\)$/ { next }
            index($0, ENVIRON["_wg_root"]) == 1 { print substr($0, length(ENVIRON["_wg_root"]) + 1) }
        ' | awk "$_CC_UNIT_KEY_AWK")
    # Recent (anything modified inside the window, the unit itself included)
    # and socket-holding units, from ONE walk.
    #
    # Read WITHOUT field splitting: IFS=' ' would trim the key, so a unit named
    # "trail " was recorded as "trail", its recent file never matched, and it
    # was reaped (and a unit named " " produced an EMPTY key, which aborts the
    # daemon under set -e). Found by review, reproduced before fixing.
    local line kind
    while IFS= read -r line; do
        kind="${line%% *}"; key="${line#? }"
        [[ -n "$key" ]] || continue
        if [[ "$kind" == s ]]; then socket[$key]=1; else recent[$key]=1; fi
    done < <(find "$root" -mindepth 1 \( -type s -printf 's %P\n' \) -o \( -mmin "-$age" -printf 'r %P\n' \) 2>/dev/null \
        | awk '{ kind = substr($0, 1, 1); p = substr($0, 3); n = split(p, c, "/")
                 if (c[1] ~ /^claude-[0-9]+$/) { if (n >= 3) print kind " " c[1] "/" c[2] "/" c[3] }
                 else if (c[1] != "") print kind " " c[1] }')
    while IFS= read -r -d '' u; do
        key="${u#"$root"/}"
        # A name with a newline cannot be represented in the line-based tables
        # above, so it is never provably unused: keep it.
        if [[ "$key" == *$'\n'* || -n "${held[$key]:-}" || -n "${recent[$key]:-}" || -n "${socket[$key]:-}" || -S "$u" ]]; then
            kept=$(( kept + 1 ))
            continue
        fi
        if (( WATCHGOD_ACT == 0 )); then
            log INFO "OBSERVE: cc-tmp sweep would reap ${u}"
        else
            rm -rf -- "$u" 2>/dev/null || log WARN "cc-tmp sweep could not remove ${u}"
        fi
        n=$(( n + 1 ))
    done < <(_cc_sweep_units "$root")
    # Project directories a sweep emptied (never a container, never recent).
    if (( WATCHGOD_ACT )); then
        local c
        for c in "$root"/claude-*; do
            [[ "${c##*/}" =~ ^claude-[0-9]+$ && -d "$c" ]] || continue
            find "$c" -mindepth 1 -maxdepth 1 -type d -empty -mmin "+$age" -delete 2>/dev/null || true
        done
    fi
    if (( n > 0 )); then
        log INFO "cc-tmp sweep (${why}, age>$(( age / 1440 ))d): $( (( WATCHGOD_ACT )) && echo reaped || echo "would reap" ) ${n} unit(s), kept ${kept}"
    fi
    return 0
}

maybe_sweep_cc_tmp() {
    # $1 = hourly | pressure. Rate-limited by a mode-scoped stamp per kind, so
    # an observe-mode sweep never uses up the first acting one. The stamp limits
    # ATTEMPTS, deliberately, unlike the reclaim start: a sweep that refused to
    # run (blind liveness) would otherwise retry — and warn — every 5 s poll.
    local kind="$1" every age
    if [[ "$kind" == pressure ]]; then
        every=$PRESSURE_RETRIGGER_S; age=$CC_SWEEP_PRESSURE_AGE_MIN
    else
        every=$CC_SWEEP_INTERVAL_S; age=$CC_SWEEP_AGE_MIN
    fi
    _wg_due "cc_sweep_${kind}" "$every" || return 0
    _wg_stamp "cc_sweep_${kind}"
    sweep_cc_tmp "$age" "$kind"
}

# ── Per-filesystem tier handling ─────────────────────────────
handle_fs() {
    # $1 path $2 limit-domain key (see check_disks) $3 tier $4 free $5 total
    # $6 eta $7 writers $8 fstype
    local path="$1" dev="$2" tier="$3" free="$4" total="$5" eta="$6" writers="$7" fstype="$8"
    local is_home=0 is_cc=0 body rel lever
    # Levers act only on the domain they relieve. check_disks sets the keys;
    # a direct call (tests) falls back to device numbers.
    [[ "$dev" == "${HOME_KEY:-$(home_dev)}" ]] && is_home=1
    [[ "$dev" == "${CC_KEY:-$(stat -c %d -- "$CC_TMP_DIR" 2>/dev/null || echo -)}" ]] && is_cc=1
    # Pages dedupe per (domain, tier, episode) — and per MODE (_wg_mode_tag): an
    # observe-mode poll records its would-be page under its own marker, so
    # flipping WATCHGOD_ACT 0→1 mid-episode still delivers the real page.
    local pg
    pg="$(_wg_mode_tag)"

    if [[ "$tier" == green ]]; then
        episode_clear "$dev"
        (( is_home )) && reserve_ensure "$free" "$total"
        return 0
    fi

    local summary="${path}: ${free} MB free of ${total} MB (${fstype}); time to full: ${eta} min"

    if ! episode_seen "$dev" yellow; then
        episode_mark "$dev" yellow
        log_attribution "$path" "$dev" "$tier" "$writers"
    fi

    if [[ "$tier" == orange || "$tier" == red ]]; then
        (( is_home )) && start_pressure_unit standard
        (( is_cc )) && maybe_sweep_cc_tmp pressure
        if [[ "$tier" == orange ]] && ! episode_seen "$dev" "orange$pg"; then
            lever="has no lever on this filesystem"
            (( is_home )) && lever="started (genesis-disk-hygiene-pressure@standard)"
            (( is_cc )) && lever="ran: cc-tmp retention sweep at 2 days"
            body="${summary}. Reclaim ${lever}. Top writers:"$'\n'"${writers:-none measurable}"
            _wg_page warning "Disk filling: ${path} ORANGE" "$body" "watchgod:disk:${dev}:orange" "WARNING — ${summary}" \
                && episode_mark "$dev" "orange$pg"
        fi
    fi

    if [[ "$tier" == red ]]; then
        rel="not applicable (reserve lives on the \$HOME filesystem)"
        if (( is_home )); then
            rel="$(reserve_release)"
            start_pressure_unit last-resort
        fi
        if ! episode_seen "$dev" "red$pg"; then
            body="${summary}. Reserve: ${rel}."$'\n'"Top writers:"$'\n'"${writers:-none measurable}"
            _wg_page emergency "Disk nearly full: ${path} RED" "$body" "watchgod:disk:${dev}:red" "EMERGENCY — ${summary}" \
                && episode_mark "$dev" "red$pg"
        fi
    fi
    return 0
}

# ── One poll ─────────────────────────────────────────────────
_IO_PREV=""
_IO_PREV_T=0
ALL_WRITERS=""
DISK_JSON=""
CC_COMPAT=""
SYS_COMPAT=""
NEXT_POLL=$POLL_INTERVAL

check_disks() {
    local now io_now writers dt p dev m free total quota unalloc meta fstype
    local used binding rate eta floor etat tier fast=0
    now="$(date +%s)"
    io_now="$(dg_io_snapshot)"
    dt=$(( now - _IO_PREV_T ))
    writers=""; ALL_WRITERS=""
    if [[ -n "$_IO_PREV" ]]; then
        ALL_WRITERS="$(dg_io_top "$_IO_PREV" "$io_now" "$dt" 100000)"
        # awk reads everything: `| head` would SIGPIPE printf on a long list
        # and, under pipefail, abort the daemon.
        writers="$(awk 'NR <= 5' <<< "$ALL_WRITERS")"
    fi
    _IO_PREV="$io_now"; _IO_PREV_T="$now"

    DISK_JSON=""; CC_COMPAT=""; SYS_COMPAT=""
    HOME_KEY=""; CC_KEY=""

    # Pass 1: measure EVERY watched path, then group paths into LIMIT DOMAINS.
    # A device number alone is not a domain: an incus dir-pool volume with a
    # project quota shares its device with the root filesystem, and statvfs
    # reports the quota for paths inside it (review finding). Paths on one
    # device are one domain, keyed by device + MOUNT POINT ("<dev>m<cksum>"),
    # which does not depend on poll order. A path whose raw statvfs size
    # differs from its own mount's by more than 1 % is under a separate limit
    # (a project quota that is not its own mount) and is keyed
    # "<dev>q<size_mb>" instead. Sizes alone are not an identity: a ZFS
    # dataset's reported size moves with its neighbours' usage, and a
    # size-based key re-keyed the domain every poll — a new page each time
    # (review finding, round 3). So the two sizes come from ONE stat call
    # (dg_raw_sizes_mb), are compared with a 1 % tolerance, and a key's files
    # outlive a one-poll disappearance (the GC below). btrfs subvolumes already
    # get distinct device numbers (MEASURED: / is 59 and the cc-tmp volume 60
    # on a live install). Known limits: a non-mount project quota within 1 %
    # of its mount's size merges with the mount, and two such quotas of the
    # same size on one device share a key (only the first path is tiered).
    local -a order=()
    local -A key_of=() meas_of=() path_of=()
    local key used_raw mnt mtotal ck
    while IFS= read -r p; do
        dev="$(stat -c %d -- "$p" 2>/dev/null)" || continue
        if ! m="$(dg_measure "$p")"; then
            _wg_warn_once "measure_$dev" "cannot measure ${p} (statvfs failed) — this filesystem is NOT being watched"
            continue
        fi
        mnt="$(stat -c %m -- "$p" 2>/dev/null)" || mnt=""
        read -r total mtotal < <(dg_raw_sizes_mb "$p" "${mnt:-$p}")
        if [[ -n "$mnt" ]] && dg_same_size "$total" "$mtotal"; then
            read -r ck _ < <(printf '%s' "$mnt" | cksum)
            key="${dev}m${ck}"
        else
            key="${dev}q${total}"
        fi
        key_of[$p]="$key"
        [[ -n "${meas_of[$key]:-}" ]] && continue
        meas_of[$key]="$m"; path_of[$key]="$p"; order+=("$key")
    done < <(watched_paths)
    HOME_KEY="${key_of[$HOME]:-}"
    CC_KEY="${key_of[$CC_TMP_DIR]:-}"
    local tmp_key="${key_of[/tmp]:-}"

    # Episode markers of a key that is no longer a domain (a quota resized
    # away, a device renumbered at boot) would silently suppress that key's
    # pages if it ever returned; rate series would accumulate forever. Both
    # are dropped — but only once the key has been gone an hour (its rate file,
    # rewritten every poll while it exists, is that old), so a key that
    # vanishes for one poll (a path briefly unmeasurable) keeps its episode.
    # Keys hold no "_", so the key is everything between the first two.
    local ef ek
    if (( ${#order[@]} )); then
        for ef in "$DG_STATE_DIR"/episode_* "$DG_STATE_DIR"/rate_*; do
            [[ -e "$ef" ]] || continue
            ek="${ef##*/}"; ek="${ek#*_}"; ek="${ek%%_*}"
            [[ -n "${meas_of[$ek]:-}" ]] && continue
            [[ -n "$(find "$DG_STATE_DIR" -maxdepth 1 -name "rate_${ek}_*" -mmin -60 -print -quit 2>/dev/null)" ]] \
                && continue
            rm -f -- "$ef" 2>/dev/null || true
        done
    fi

    # Pass 2: tier and act, once per domain.
    for key in "${order[@]}"; do
        p="${path_of[$key]}"
        read -r free total quota unalloc meta fstype used_raw <<< "${meas_of[$key]}"
        used=$(( total - free ))
        [[ "$used_raw" =~ ^[0-9]+$ ]] || used_raw=$used
        binding=fs; (( quota )) && binding=quota
        rate="$(dg_rate_update "$key" "$used_raw" "$now" "$binding")"
        eta="$(dg_eta_min "$free" "$rate")"
        floor="$(dg_floor_tier "$free" "$total" "$unalloc" "$meta")"
        etat="$(dg_eta_tier "$eta")"
        tier="$(dg_tier "$floor" "$etat")"
        [[ "$tier" == orange || "$tier" == red || "$etat" != green ]] && fast=1

        handle_fs "$p" "$key" "$tier" "$free" "$total" "$eta" "$writers" "$fstype"

        [[ -n "$DISK_JSON" ]] && DISK_JSON+=", "
        DISK_JSON+="$(_wg_json_str "$p"): {\"tier\": \"$tier\", \"floor_tier\": \"$floor\", \"free_mb\": $free, \"total_mb\": $total, \"used_pct\": $(( total > 0 ? used * 100 / total : 0 )), \"eta_min\": $([[ "$eta" == - ]] && echo null || echo "$eta"), \"rate_mb_per_min\": $rate, \"quota\": $([[ $quota == 1 ]] && echo true || echo false), \"unalloc_mb\": $([[ "$unalloc" == - ]] && echo null || echo "$unalloc"), \"meta_pct\": $([[ "$meta" == - ]] && echo null || echo "$meta"), \"fstype\": $(_wg_json_str "$fstype")}"

        # Compat for readers of the v1 keys. The FLOOR tier, deliberately: the
        # cc_tmp tier drives routing degradation (TmpPressureStatus), and an
        # ETA-driven YELLOW on an ordinary large download must not shed call
        # sites. The last field says whether these figures are cc-tmp's OWN
        # (a domain separate from $HOME's on the same device, or a mount whose figures are its own —
        # its quota binds, or it is not btrfs, where a subvolume's statvfs is
        # the whole pool's): only then is total-free cc-tmp's usage (review
        # finding — a quota on a SHARED root subvolume would count every other
        # file as cc-tmp's).
        if [[ -n "$CC_KEY" && "$key" == "$CC_KEY" ]]; then
            local own=0
            if [[ "$key" == "$HOME_KEY" ]]; then
                own=0   # shares $HOME's domain: total-free is everyone's files
            elif [[ -n "$HOME_KEY" && "$key" == *q* && "${key%%[qm]*}" == "${HOME_KEY%%[qm]*}" ]]; then
                own=1   # same device, its own limit (a project quota)
            elif mountpoint -q -- "$CC_TMP_DIR" 2>/dev/null && [[ "$quota" == 1 || "$fstype" != btrfs ]]; then
                own=1
            fi
            CC_COMPAT="$floor $free $total $own"
        fi
        if [[ -n "$tmp_key" && "$key" == "$tmp_key" ]]; then SYS_COMPAT="$floor $free $total $fstype"; fi
    done

    NEXT_POLL=$POLL_INTERVAL
    (( fast )) && NEXT_POLL=$FAST_POLL_INTERVAL
    return 0
}

write_state() {
    local cc_tier=unknown cc_free=0 cc_total=0 cc_own=0 cc_used=0
    local sys_tier=unknown sys_free=0 sys_total=0 sys_fstype=- sys_pct=0 is_tmpfs=false
    if [[ -n "$CC_COMPAT" ]]; then
        read -r cc_tier cc_free cc_total cc_own <<< "$CC_COMPAT"
        if (( cc_own )); then
            cc_used=$(( cc_total - cc_free ))
        else
            # On a shared filesystem total-free is the whole disk, not cc-tmp,
            # so du it — at most every 5 minutes, not every (fast) poll.
            local now_s cache="$DG_STATE_DIR/cc_used_du" cached_t cached_v
            now_s="$(date +%s)"
            if read -r cached_t cached_v 2>/dev/null < "$cache" \
                    && [[ "$cached_t" =~ ^[0-9]+$ && "$cached_v" =~ ^[0-9]+$ ]] \
                    && (( cached_t <= now_s && now_s - cached_t < 300 )); then
                cc_used=$cached_v
            else
                # du exits 1 when ANY entry is unreadable yet still prints the
                # total; under pipefail an `|| cc_used=""` would throw that total
                # away and cache 0 (review finding). Keep whatever number came out.
                cc_used="$(timeout 20 du -smx -- "$CC_TMP_DIR" 2>/dev/null | cut -f1)" || true
                cc_used="${cc_used%%$'\n'*}"
                [[ "$cc_used" =~ ^[0-9]+$ ]] || cc_used=0
                { mkdir -p "$DG_STATE_DIR" && echo "$now_s $cc_used" > "$cache"; } 2>/dev/null || true
            fi
        fi
    fi
    if [[ -n "$SYS_COMPAT" ]]; then
        read -r sys_tier sys_free sys_total sys_fstype <<< "$SYS_COMPAT"
        (( sys_total > 0 )) && sys_pct=$(( (sys_total - sys_free) * 100 / sys_total ))
        [[ "$sys_fstype" == tmpfs ]] && is_tmpfs=true
    fi
    local tmp="${STATE_FILE}.tmp"
    cat 2>/dev/null > "$tmp" <<EOF || { rm -f "$tmp" 2>/dev/null; return 0; }
{
  "disk": {${DISK_JSON}},
  "act": ${WATCHGOD_ACT},
  "cc_tmp": {"tier": "$cc_tier", "used_mb": $cc_used, "budget_mb": $cc_total, "sacred_mb": 0, "fs_free_mb": $cc_free, "fs_total_mb": $cc_total},
  "system_tmp": {"tier": "$sys_tier", "used_pct": $sys_pct, "is_tmpfs": $is_tmpfs},
  "poll_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
    mv "$tmp" "$STATE_FILE" 2>/dev/null || true
}

# OOM event capture lives in its own file (moved verbatim; see its header).
# shellcheck source=scripts/lib/watchgod_oom.sh
source "$_SCRIPT_DIR/lib/watchgod_oom.sh"


# ── Main loop ────────────────────────────────────────────────
main() {
    mkdir -p "$(dirname "$LOG_FILE")" "$ALERT_DIR"
    load_config
    mkdir -p "$DG_STATE_DIR"
    # v1's shared tier flags. Nothing reads them, and v2 never clears them, so
    # a leftover would sit there forever looking like a live alarm.
    rm -f "$ALERT_DIR/tmp_warning" "$ALERT_DIR/tmp_emergency" "$ALERT_DIR/tmp_orange_stuck" 2>/dev/null || true
    log INFO "Watchgod v2 starting (poll=${POLL_INTERVAL}s, fast=${FAST_POLL_INTERVAL}s, act=${WATCHGOD_ACT}, downloads=${DOWNLOADS_DIR})"
    (( WATCHGOD_ACT )) || log WARN "OBSERVE mode (WATCHGOD_ACT=0): tiers are measured and logged; nothing is reclaimed, released or paged"

    # Baseline the OOM counter at startup so we only page on NEW kills (never the
    # cumulative-since-boot history). Empty baseline = monitoring unavailable.
    local oom_baseline
    oom_baseline=$(_oom_arm_baseline)
    if [[ -z "$oom_baseline" ]]; then
        log INFO "OOM event capture unavailable (no readable ${OOM_EVENTS_FILE}) — OOM monitoring off"
    else
        log INFO "OOM event capture armed (baseline oom_kill=${oom_baseline%%:*})"
    fi

    while true; do
        load_config

        check_disks
        write_state
        maybe_sweep_cc_tmp hourly

        # Durable OOM capture — snapshot + page on any NEW cgroup OOM kill.
        oom_baseline=$(check_oom_events "$oom_baseline")

        # Log rotation — truncate when > 1MB
        local log_size
        log_size=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
        if (( log_size > 1048576 )); then
            { tail -100 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"; } 2>/dev/null || true
        fi

        sleep "$NEXT_POLL"
    done
}

# Every tunable now holds its code default or environment override: that is the
# baseline load_config restores before each re-read.
_wg_snapshot_defaults

# Run the poll loop only when executed directly — sourcing (e.g. from tests) loads the
# functions without starting the daemon.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
