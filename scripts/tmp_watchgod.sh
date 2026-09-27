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
#   RED     release the reserve file, freeze a runaway DOWNLOADER writing into
#           ~/tmp/downloads (SIGSTOP; `scripts/watchgod thaw` resumes it),
#           start genesis-disk-hygiene-pressure@last-resort, page EMERGENCY.
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
# A candidate below this write rate (MB/min) is not the runaway.
DG_FREEZE_MIN_RATE="${DG_FREEZE_MIN_RATE:-60}"
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
    DG_FREEZE_MIN_RATE DG_FREEZE_ALLOW_COMMS PRESSURE_RETRIGGER_S DG_ATTRIBUTION_PATHS
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
    # Every numeric threshold reaches shell arithmetic, where a hand-edited
    # "10%" or "abc" is an unset-variable abort under set -u — the daemon would
    # die over a typo. Reset each bad value to its default, loudly.
    local kv k d
    for kv in DG_YELLOW_PCT:15 DG_ORANGE_PCT:8 DG_RED_PCT:3 DG_RED_MIN_MB:3072 \
              DG_ETA_YELLOW_MIN:360 DG_ETA_ORANGE_MIN:60 \
              DG_ETA_RED_MIN:10 DG_META_RED_PCT:80 DG_UNALLOC_RED_MB:1024; do
        k="${kv%%:*}"; d="${kv##*:}"
        if [[ ! "${!k:-}" =~ ^[0-9]+$ ]]; then
            _wg_warn_once "bad_$k" "watchgod.conf: ${k}='${!k:-}' is not a whole number — using ${d}"
            printf -v "$k" '%s' "$d"
        fi
    done
    [[ "$RESERVE_MAX_MB" =~ ^[0-9]+$ ]] || RESERVE_MAX_MB=2048
    [[ "$DG_FREEZE_MIN_RATE" =~ ^[0-9]+$ ]] || DG_FREEZE_MIN_RATE=60
    [[ "$PRESSURE_RETRIGGER_S" =~ ^[0-9]+$ ]] || PRESSURE_RETRIGGER_S=600
    [[ "$CC_SWEEP_INTERVAL_S" =~ ^[0-9]+$ ]] || CC_SWEEP_INTERVAL_S=3600
    # A retention age under a day would reap an idle session's temp while its
    # owner is at lunch; refuse it rather than obey it.
    [[ "$CC_SWEEP_AGE_MIN" =~ ^[0-9]+$ ]] && (( CC_SWEEP_AGE_MIN >= 1440 )) || CC_SWEEP_AGE_MIN=10080
    [[ "$CC_SWEEP_PRESSURE_AGE_MIN" =~ ^[0-9]+$ ]] && (( CC_SWEEP_PRESSURE_AGE_MIN >= 1440 )) \
        || CC_SWEEP_PRESSURE_AGE_MIN=2880
}

_wg_canon() {
    local d="$1"
    while [[ "$d" == */ && "$d" != "/" ]]; do d="${d%/}"; done
    printf '%s' "$d"
}

# ── Logging ──────────────────────────────────────────────────
log() {
    local level="$1"; shift
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [$level] $*" >> "$LOG_FILE"
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
# attribution fire once per (filesystem, tier) per episode; the freeze and the
# reserve re-evaluate on every RED poll, because a new runaway can start
# mid-episode.
_ep_file() { printf '%s/episode_%s_%s' "$DG_STATE_DIR" "$1" "$2"; }
episode_seen() { [[ -f "$(_ep_file "$1" "$2")" ]]; }
episode_mark() { mkdir -p "$DG_STATE_DIR"; : > "$(_ep_file "$1" "$2")"; }
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
        if [[ "$dev" == "$(home_dev)" ]]; then
            while IFS= read -r t; do
                [[ "$(stat -c %d -- "$t" 2>/dev/null)" == "$dev" ]] && targets+=("$t")
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
    mkdir -p "$DG_STATE_DIR"
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

# ── Reclaim (the one deleter lives in disk_hygiene.sh) ───────
start_pressure_unit() {
    # $1 = standard | last-resort. Rate-limited per instance while a tier
    # persists; systemd itself refuses a second concurrent run.
    local inst="$1" stamp now last=0 unit
    unit="genesis-disk-hygiene-pressure@${inst}.service"
    stamp="$DG_STATE_DIR/pressure_${inst}"
    now="$(date +%s)"
    [[ -f "$stamp" ]] && last="$(cat "$stamp" 2>/dev/null || echo 0)"
    [[ "$last" =~ ^[0-9]+$ ]] || last=0
    (( now - last >= PRESSURE_RETRIGGER_S )) || return 0
    if (( WATCHGOD_ACT == 0 )); then
        log WARN "OBSERVE: would start ${unit}"
        return 0
    fi
    mkdir -p "$DG_STATE_DIR"; echo "$now" > "$stamp"
    if systemctl --user start --no-block "$unit" 2>/dev/null; then
        log WARN "started ${unit}"
    else
        log WARN "could not start ${unit} — is it rendered? (bootstrap renders scripts/systemd/*.template)"
    fi
    return 0
}

# ── Freeze (narrow, reversible) ──────────────────────────────
FROZEN_FILE="$DG_STATE_DIR/frozen"

protected_pids() {
    local u
    printf '%s ' "$$" "$PPID"
    for u in genesis-server.service genesis-bridge.service; do
        systemctl --user show -p MainPID --value "$u" 2>/dev/null | tr '\n' ' ' || true
    done
    return 0
}

freeze_runaways() {
    # $1 device in trouble, $2 EVERY writer measured this poll ("pid st delta
    # rate comm"). Candidates are ALL of them, not the attribution top five:
    # MEASURED in the E2E on a busy box, a 300 MB/min downloader never reached
    # the top five behind test runs and a database at 400-870 MB/min, so a
    # top-N filter would never have looked at it. Echoes a summary for the page.
    local dev="$1" writers="$2" pid st _delta rate comm why prot out="" c known
    # Nothing to freeze unless the downloads directory is ON this filesystem.
    [[ -d "$DOWNLOADS_DIR" && "$(stat -c %d -- "$DOWNLOADS_DIR" 2>/dev/null)" == "$dev" ]] || return 0
    prot="$(protected_pids)"
    while read -r pid st _delta rate comm; do
        [[ -n "${pid:-}" ]] || continue
        (( rate >= DG_FREEZE_MIN_RATE )) || continue
        # The allowlist first: cheap, and most heavy writers are not downloaders.
        known=0
        for c in $DG_FREEZE_ALLOW_COMMS; do [[ "$c" == "$comm" ]] && known=1; done
        if (( ! known )); then
            [[ -n "${DG_FREEZE_DRY:-}" ]] && out+="not on the downloader allowlist: pid ${pid} (${comm}, ${rate} MB/min)"$'\n'
            continue
        fi
        if why="$(dg_freeze_eligible "$pid" "$st" "$comm" "$DOWNLOADS_DIR" "$prot" "$dev")"; then
            if [[ -n "${DG_FREEZE_DRY:-}" ]]; then
                out+="would freeze pid ${pid} (${comm}, ${rate} MB/min)"$'\n'
                continue
            fi
            if (( WATCHGOD_ACT == 0 )); then
                _wg_warn_once "wf_${pid}_${st}" "OBSERVE: would freeze pid ${pid} (${comm}, ${rate} MB/min) writing into ${DOWNLOADS_DIR}"
                out+="OBSERVE: would freeze pid ${pid} (${comm}, ${rate} MB/min)"$'\n'
                continue
            fi
            if kill -STOP "$pid" 2>/dev/null; then
                mkdir -p "$DG_STATE_DIR"
                # Same lock `scripts/watchgod thaw` rewrites the file under, so
                # a thaw cannot drop a record appended mid-rewrite.
                (
                    flock 9
                    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$pid" "$st" "$comm" "$(date -u +%FT%TZ)" "$dev" "$rate" >> "$FROZEN_FILE"
                ) 9>"$FROZEN_FILE.lock"
                log WARN "FROZE pid ${pid} (${comm}) writing ${rate} MB/min into ${DOWNLOADS_DIR}; resume with: scripts/watchgod thaw ${pid}"
                # Its own page: the episode's RED page can predate the freeze
                # (a writer is only measurable from the second poll), and the
                # operator must learn WHAT stopped and how to resume it.
                queue_alert emergency "watchgod:disk" "Froze a runaway download (pid ${pid}, ${comm})" \
                    "The disk is nearly full and ${comm} (pid ${pid}) was writing ${rate} MB/min into ${DOWNLOADS_DIR}. It is PAUSED, not killed. Resume it once there is room: scripts/watchgod thaw ${pid} — or end it: kill ${pid}. Its remote end or parent may time out while it waits." \
                    "watchgod:disk:${dev}:freeze:${pid}:${st}:$(date +%s)"
                out+="FROZE pid ${pid} (${comm}, ${rate} MB/min) — resume: scripts/watchgod thaw ${pid}"$'\n'
            fi
        else
            out+="downloader not frozen: pid ${pid} (${comm}, ${rate} MB/min) — ${why}"$'\n'
        fi
    done <<< "$writers"
    printf '%s' "$out"
}

frozen_count() {
    # Processes STILL frozen: a record whose pid is gone or now names another
    # process (start time changed) is history, not a frozen process.
    local n=0 pid st _rest cur rest
    [[ -f "$FROZEN_FILE" ]] || { echo 0; return 0; }
    while IFS=$'\t' read -r pid st _rest; do
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        rest=""
        read -r rest 2>/dev/null < "$DG_PROC/$pid/stat" || continue
        rest="${rest##*) }"
        read -r -a _wg_f <<< "$rest"
        cur="${_wg_f[19]:-}"
        [[ "$cur" == "$st" ]] && n=$(( n + 1 ))
    done < "$FROZEN_FILE"
    echo "$n"
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
    snap="$(live_open_paths)"
    if [[ -z "$snap" ]]; then
        log WARN "cc-tmp sweep skipped: the liveness snapshot is empty (cannot see /proc), so nothing can be proven unused"
        return 0
    fi
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
    # $1 = hourly | pressure. Rate-limited by a stamp per kind.
    local kind="$1" stamp now last=0 every age
    if [[ "$kind" == pressure ]]; then
        every=$PRESSURE_RETRIGGER_S; age=$CC_SWEEP_PRESSURE_AGE_MIN
    else
        every=$CC_SWEEP_INTERVAL_S; age=$CC_SWEEP_AGE_MIN
    fi
    stamp="$DG_STATE_DIR/cc_sweep_${kind}"
    now="$(date +%s)"
    [[ -f "$stamp" ]] && last="$(cat "$stamp" 2>/dev/null || echo 0)"
    [[ "$last" =~ ^[0-9]+$ ]] || last=0
    (( now - last >= every )) || return 0
    mkdir -p "$DG_STATE_DIR"; echo "$now" > "$stamp"
    sweep_cc_tmp "$age" "$kind"
}

# ── Per-filesystem tier handling ─────────────────────────────
handle_fs() {
    # $1 path $2 dev $3 tier $4 free $5 total $6 eta $7 writers $8 fstype
    local path="$1" dev="$2" tier="$3" free="$4" total="$5" eta="$6" writers="$7" fstype="$8"
    local is_home=0 is_cc=0 body frz rel lever
    [[ "$dev" == "$(home_dev)" ]] && is_home=1
    [[ "$dev" == "$(stat -c %d -- "$CC_TMP_DIR" 2>/dev/null || echo -)" ]] && is_cc=1

    if [[ "$tier" == green ]]; then
        episode_clear "$dev"
        (( is_home )) && reserve_ensure "$free" "$total"
        return 0
    fi

    local summary="${path}: ${free} MB free of ${total} MB (${fstype}); time to full: ${eta} min"

    if ! episode_seen "$dev" yellow; then
        episode_mark "$dev" yellow
        log_attribution "$path" "$dev" "$tier" "$writers"
        # The evidence the broad-freeze decision waits on: what the freeze
        # WOULD do on this filesystem, logged at the start of every episode
        # (a dry run — nothing is signalled below RED).
        local dry
        dry="$(DG_FREEZE_DRY=1 freeze_runaways "$dev" "$ALL_WRITERS")"
        if [[ -n "$dry" ]]; then
            while IFS= read -r l; do log WARN "freeze candidate (dry): $l"; done <<< "$dry"
        fi
    fi

    if [[ "$tier" == orange || "$tier" == red ]]; then
        (( is_home )) && start_pressure_unit standard
        (( is_cc )) && maybe_sweep_cc_tmp pressure
        if [[ "$tier" == orange ]] && ! episode_seen "$dev" orange; then
            episode_mark "$dev" orange
            lever="has no lever on this filesystem"
            (( is_home )) && lever="started (genesis-disk-hygiene-pressure@standard)"
            (( is_cc )) && lever="ran: cc-tmp retention sweep at 2 days"
            body="${summary}. Reclaim ${lever}. Top writers:"$'\n'"${writers:-none measurable}"
            if (( WATCHGOD_ACT )); then
                queue_alert warning "watchgod:disk" "Disk filling: ${path} ORANGE" "$body" "watchgod:disk:${dev}:orange"
            else
                log WARN "OBSERVE: would page WARNING — ${summary}"
            fi
        fi
    fi

    if [[ "$tier" == red ]]; then
        rel="not applicable (reserve lives on the \$HOME filesystem)"
        if (( is_home )); then
            rel="$(reserve_release)"
            start_pressure_unit last-resort
        fi
        frz="$(freeze_runaways "$dev" "$ALL_WRITERS")"
        if ! episode_seen "$dev" red; then
            episode_mark "$dev" red
            body="${summary}. Reserve: ${rel}."$'\n'"${frz:-No freeze candidate (only known downloaders writing into ${DOWNLOADS_DIR} on this filesystem are ever frozen).}"$'\n'"Top writers:"$'\n'"${writers:-none measurable}"$'\n'"A frozen download is paused, not killed — but its remote end or parent may time out while it waits."
            if (( WATCHGOD_ACT )); then
                queue_alert emergency "watchgod:disk" "Disk nearly full: ${path} RED" "$body" "watchgod:disk:${dev}:red"
            else
                log WARN "OBSERVE: would page EMERGENCY — ${summary}"
            fi
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
    local -A seen=()
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

    local cc_dev tmp_dev
    cc_dev="$(stat -c %d -- "$CC_TMP_DIR" 2>/dev/null || echo -)"
    tmp_dev="$(stat -c %d -- /tmp 2>/dev/null || echo -)"
    DISK_JSON=""; CC_COMPAT=""; SYS_COMPAT=""

    while IFS= read -r p; do
        dev="$(stat -c %d -- "$p" 2>/dev/null)" || continue
        [[ -n "${seen[$dev]:-}" ]] && continue
        seen[$dev]=1
        if ! m="$(dg_measure "$p")"; then
            _wg_warn_once "measure_$dev" "cannot measure ${p} (statvfs failed) — this filesystem is NOT being watched"
            continue
        fi
        local used_raw
        read -r free total quota unalloc meta fstype used_raw <<< "$m"
        used=$(( total - free ))
        [[ "$used_raw" =~ ^[0-9]+$ ]] || used_raw=$used
        binding=fs; (( quota )) && binding=quota
        rate="$(dg_rate_update "$dev" "$used_raw" "$now" "$binding")"
        eta="$(dg_eta_min "$free" "$rate")"
        floor="$(dg_floor_tier "$free" "$total" "$unalloc" "$meta")"
        etat="$(dg_eta_tier "$eta")"
        tier="$(dg_tier "$floor" "$etat")"
        [[ "$tier" == orange || "$tier" == red || "$etat" != green ]] && fast=1

        handle_fs "$p" "$dev" "$tier" "$free" "$total" "$eta" "$writers" "$fstype"

        [[ -n "$DISK_JSON" ]] && DISK_JSON+=", "
        DISK_JSON+="$(_wg_json_str "$p"): {\"tier\": \"$tier\", \"floor_tier\": \"$floor\", \"free_mb\": $free, \"total_mb\": $total, \"used_pct\": $(( total > 0 ? used * 100 / total : 0 )), \"eta_min\": $([[ "$eta" == - ]] && echo null || echo "$eta"), \"rate_mb_per_min\": $rate, \"quota\": $([[ $quota == 1 ]] && echo true || echo false), \"unalloc_mb\": $([[ "$unalloc" == - ]] && echo null || echo "$unalloc"), \"meta_pct\": $([[ "$meta" == - ]] && echo null || echo "$meta"), \"fstype\": $(_wg_json_str "$fstype")}"

        # Compat for readers of the v1 keys. The FLOOR tier, deliberately: the
        # cc_tmp tier drives routing degradation (TmpPressureStatus), and an
        # ETA-driven YELLOW on an ordinary large download must not shed call
        # sites.
        if [[ "$dev" == "$cc_dev" ]]; then CC_COMPAT="$floor $free $total $quota"; fi
        if [[ "$dev" == "$tmp_dev" ]]; then SYS_COMPAT="$floor $free $total $fstype"; fi
    done < <(watched_paths)

    NEXT_POLL=$POLL_INTERVAL
    (( fast )) && NEXT_POLL=$FAST_POLL_INTERVAL
    return 0
}

write_state() {
    local cc_tier=unknown cc_free=0 cc_total=0 cc_quota=0 cc_used=0
    local sys_tier=unknown sys_free=0 sys_total=0 sys_fstype=- sys_pct=0 is_tmpfs=false
    if [[ -n "$CC_COMPAT" ]]; then
        read -r cc_tier cc_free cc_total cc_quota <<< "$CC_COMPAT"
        if (( cc_quota )); then
            cc_used=$(( cc_total - cc_free ))
        else
            # On a shared filesystem total-free is the whole disk, not cc-tmp,
            # so du it — at most every 5 minutes, not every (fast) poll.
            local now_s cache="$DG_STATE_DIR/cc_used_du" cached_t cached_v
            now_s="$(date +%s)"
            if read -r cached_t cached_v 2>/dev/null < "$cache" \
                    && [[ "$cached_t" =~ ^[0-9]+$ && "$cached_v" =~ ^[0-9]+$ ]] \
                    && (( now_s - cached_t < 300 )); then
                cc_used=$cached_v
            else
                cc_used="$(timeout 20 du -smx -- "$CC_TMP_DIR" 2>/dev/null | cut -f1)" || cc_used=""
                [[ "$cc_used" =~ ^[0-9]+$ ]] || cc_used=0
                mkdir -p "$DG_STATE_DIR"; echo "$now_s $cc_used" > "$cache" 2>/dev/null || true
            fi
        fi
    fi
    if [[ -n "$SYS_COMPAT" ]]; then
        read -r sys_tier sys_free sys_total sys_fstype <<< "$SYS_COMPAT"
        (( sys_total > 0 )) && sys_pct=$(( (sys_total - sys_free) * 100 / sys_total ))
        [[ "$sys_fstype" == tmpfs ]] && is_tmpfs=true
    fi
    local tmp="${STATE_FILE}.tmp"
    cat > "$tmp" <<EOF
{
  "disk": {${DISK_JSON}},
  "act": ${WATCHGOD_ACT},
  "frozen": $(frozen_count),
  "cc_tmp": {"tier": "$cc_tier", "used_mb": $cc_used, "budget_mb": $cc_total, "sacred_mb": 0, "fs_free_mb": $cc_free, "fs_total_mb": $cc_total},
  "system_tmp": {"tier": "$sys_tier", "used_pct": $sys_pct, "is_tmpfs": $is_tmpfs},
  "poll_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
    mv "$tmp" "$STATE_FILE"
}

# ── OOM event capture (best-effort, cgroup v2) ───────────────
# A cgroup OOM kill silently collapses a CC session (tmux `exec claude` → claude
# is reaped → the last pane dies → the session ends) and leaves no durable trace:
# the kernel dmesg ring cycles and the kernel journal is usually unreadable from
# inside the container. This samples the container cgroup's CUMULATIVE oom_kill
# counter each poll and, on a NEW kill since the daemon started, records a
# timestamped snapshot (memory + top-RSS processes) and pages once. Read-only —
# it never kills or reclaims anything. Degrades to a no-op when the cgroup-v2
# interface file is absent/unreadable (older layouts / non-cgroup2 hosts).

_read_oom_kill() {
    # Echo the current cumulative oom_kill count; non-zero return if unavailable.
    [[ -r "$OOM_EVENTS_FILE" ]] || return 1
    awk '/^oom_kill /{print $2; found=1} END{exit !found}' "$OOM_EVENTS_FILE" 2>/dev/null
}

_read_oom_local_trigger() {
    # Echo the container root's LOCAL `oom` count (limit invocations charged to
    # this cgroup itself, descendants excluded); non-zero return if unavailable.
    local _local_file="${OOM_EVENTS_LOCAL_FILE:-$(dirname "$OOM_EVENTS_FILE")/memory.events.local}"
    [[ -r "$_local_file" ]] || return 1
    awk '/^oom /{print $2; found=1} END{exit !found}' "$_local_file" 2>/dev/null
}

# Attribution reads the systemd journal because the killer cgroup is usually a
# TRANSIENT scope, deleted with its job — every surviving cgroup shows the kill
# only as an inherited aggregate (measured: local=0 at every level) — while the
# journal names the unit and outlives the scope. The query window is a CURSOR:
# each successful read advances a durable epoch marker, and the next read asks
# only for lines SINCE it. That is what keeps attribution honest during a
# thrashing contained job: without it, a contained kill's line from the
# PREVIOUS increment still inside a fixed lookback could account for a NEW
# kill that left no line of its own (a non-main process dying inside a
# surviving scope writes no unit-failure line) and silence a page. A missing
# cursor (first run) falls back to a short lookback computed from the LIVE
# poll interval; a failed query does not advance the cursor. Every failure
# direction lands on the unattributed PAGE, never on silence.
_OOM_CURSOR_FILE="$(dirname "$LOG_FILE")/.oom_journal_cursor"

_oom_killed_units() {
    # Echo unit names the user journal says were oom-killed since the cursor,
    # one per line.
    # rc!=0 = journal UNAVAILABLE (no journalctl, or the query failed) — the
    # caller degrades to the unattributed page. rc=0 with empty output =
    # journal readable, no oom-kill record (also unattributed).
    command -v journalctl >/dev/null 2>&1 || return 1
    # Computed per call, not at load time: load_config re-sources watchgod.conf
    # every tick and may change POLL_INTERVAL — a frozen window shorter than
    # one poll gap would miss every contained kill and re-open the false pages.
    local _fallback_s=$(( POLL_INTERVAL * 2 + 60 ))
    local _cursor out rc=0
    _cursor=$(cat "$_OOM_CURSOR_FILE" 2>/dev/null) || _cursor=""
    # A REAL journal cursor, not a timestamp. `--since` is a TIMESTAMP filter and
    # is INCLUSIVE at its boundary, so an entry landing exactly on the stored
    # second is re-read on the next tick; `--after-cursor` is a POSITION filter
    # and starts strictly AFTER the named entry, so every record is seen exactly
    # once. That distinction is load-bearing now that the caller reconciles
    # RECORD COUNT against kill deltas: a double-counted boundary entry would
    # inflate the count. `--show-cursor` appends a trailing `-- cursor: s=…`
    # line, stripped below.
    #
    # NOTE (adversarial audit, #1790): journalctl REFUSES to combine
    # --after-cursor with --since/--cursor ("Please specify only one of" —
    # verified on systemd 255), and these records' timestamps are PID 1's
    # EMISSION time, not the kernel kill time — so a time window can neither
    # compose with the position filter nor bound a late record anyway. The
    # late/pre-baseline record problem is therefore closed by the caller's
    # DEFICIT reconciliation (see check_oom_events), not here.
    if [[ "$_cursor" == s=* ]]; then
        out=$(journalctl --user --after-cursor "$_cursor" --no-pager --show-cursor -o cat 2>/dev/null) || rc=$?
    else
        # First run, or a cursor file written by an older version (epoch digits):
        # fall back to the time window. Never trust a malformed value as a cursor.
        out=$(journalctl --user --since "-${_fallback_s} seconds" --no-pager --show-cursor -o cat 2>/dev/null) || rc=$?
    fi
    [[ $rc -ne 0 ]] && return 1
    # Advance the cursor only on a SUCCESSFUL read (this function runs in a
    # command substitution, but file writes escape the subshell). If the read
    # returned no cursor line (an empty journal window), KEEP the old cursor
    # rather than clearing it — clearing would re-read the whole window next
    # tick and double-count.
    local _newcur
    _newcur=$(printf '%s\n' "$out" | sed -n 's/^-- cursor: //p' | tail -1)
    # Through the single verified writer, like every other advance. On failure
    # the OLD cursor is removed rather than left behind: a stale value is
    # indistinguishable from a fresh anchor to anything that merely checks the
    # file exists, and `drain` is cleared on exactly that check.
    if [[ -n "$_newcur" ]]; then
        _oom_persist_cursor "$_newcur" || rm -f "$_OOM_CURSOR_FILE" 2>/dev/null || true
    fi
    # `-o cat` renders systemd's line as `<unit>: Failed with result 'oom-kill'.`
    # A unit name can legally contain ':' (template instances); cut would then
    # truncate it, and a truncated name cannot match a contained prefix — so a
    # pathological name mis-classifies toward PAGING, the safe direction.
    # NOT `sort -u`: the caller compares this list's RECORD COUNT against the
    # kill delta, and de-duplicating collapses two kills of the same unit name
    # into one line — which would under-count and suppress a page for a kill
    # nothing accounted for. Cardinality is the point; the display string
    # de-duplicates separately. (The `-- cursor:` line carries no oom-kill
    # phrase, so grep drops it here.)
    printf '%s
' "$out"         | { grep -F ": Failed with result 'oom-kill'" || true; }         | cut -d: -f1
}

_oom_units_all_contained() {
    # $1 = newline-separated non-empty unit list. rc 0 = EVERY unit matches a
    # contained prefix; any unmatched unit → rc 1 (one uncontained kill pages).
    local u p ok
    while IFS= read -r u; do
        [[ -z "$u" ]] && continue
        ok=0
        for p in $OOM_CONTAINED_UNIT_PREFIXES; do
            [[ "$u" == "$p"* ]] && { ok=1; break; }
        done
        [[ $ok -eq 1 ]] || return 1
    done <<<"$1"
    return 0
}

check_oom_events() {
    # $1 = the carried baseline spec
    #   counter:local_oom:deficit:deficit_ts:drain
    # (a bare counter from an older caller is accepted: local unverified,
    # deficit 0). Echoes the refreshed spec for the next tick. Never touches
    # stdout except the final spec echo.
    #
    # The spec carries FIVE facts because suppression needs all of them
    # (#1790 review round):
    #   counter     — the oom_kill aggregate (was there a kill?)
    #   local_oom   — the container root's LOCAL oom count (WHOSE limit fired:
    #                 the journal names the victim unit, and a container-limit
    #                 kill can victimise a contained child scope)
    #   deficit     — kills we already paged for whose journal records have not
    #                 been seen yet. systemd's record of a unit failure can be
    #                 emitted AFTER the poll that observed the counter
    #                 increment, and the record's timestamp is PID 1's EMISSION
    #                 time (measured on the live journal, #1790 audit) — so NO
    #                 time window can exclude a late record from the next
    #                 kill's batch. The only sound correlation is
    #                 reconciliation: records returned by a query first retire
    #                 the owed deficit, and only the REST may account for the
    #                 current delta. A late record can therefore never cover a
    #                 kill it does not belong to (Codex P1 / Devin, #1790).
    #   deficit_ts  — when the deficit last GREW. A kill that never writes a
    #                 record (a non-main process dying inside a surviving
    #                 scope) leaves a deficit nothing can retire; it expires
    #                 after 20 poll intervals so contained kills are not
    #                 spuriously paged forever. Residue, accepted: a journald
    #                 outage LONGER than the TTL, ending with a query whose
    #                 backlogged records exactly equal deficit+n, can
    #                 mis-attribute once. systemd's emission lag is
    #                 milliseconds in every measurement we have; the TTL is
    #                 generous against it.
    #   drain       — set when arming could not advance the journal cursor
    #                 (journalctl absent/failing at startup): the FIRST
    #                 resolution must page and re-anchor, because the fallback
    #                 window would return pre-baseline records that could
    #                 otherwise "account" for a post-startup kill.
    local prev_spec="$1" prev prev_local prev_deficit prev_deficit_ts prev_drain
    prev="${prev_spec%%:*}"
    local _r1="" _r2="" _r3="" _r4=""
    [[ "$prev_spec" == *:* ]] && _r1="${prev_spec#*:}"
    prev_local="${_r1%%:*}"
    [[ "$_r1" == *:* ]] && _r2="${_r1#*:}"
    prev_deficit="${_r2%%:*}"
    [[ "$_r2" == *:* ]] && _r3="${_r2#*:}"
    prev_deficit_ts="${_r3%%:*}"
    [[ "$_r3" == *:* ]] && _r4="${_r3#*:}"
    prev_drain="${_r4%%:*}"
    # Numeric hygiene: a malformed element degrades to "unknown", never to a
    # bash arithmetic error under set -e (audit NOTE, #1790).
    [[ "$prev" =~ ^[0-9]+$ ]] || prev=""
    [[ "$prev_local" =~ ^[0-9]+$ ]] || prev_local=""
    [[ "$prev_deficit" =~ ^[0-9]+$ ]] || prev_deficit=0
    [[ "$prev_deficit_ts" =~ ^[0-9]+$ ]] || prev_deficit_ts=0
    [[ "$prev_drain" == "1" ]] || prev_drain=0
    local cur loc_oom now_epoch
    cur=$(_read_oom_kill) || { printf '%s' "$prev_spec"; return 0; }
    [[ "$cur" =~ ^[0-9]+$ ]] || { printf '%s' "$prev_spec"; return 0; }
    loc_oom=$(_read_oom_local_trigger) || loc_oom=""
    [[ "$loc_oom" =~ ^[0-9]+$ ]] || loc_oom=""
    now_epoch=$(date +%s)
    # Expire an unretireable deficit (see the spec comment above). Computed
    # PER CALL, like _oom_killed_units' fallback window: load_config re-sources
    # watchgod.conf every tick and may change POLL_INTERVAL.
    local _deficit_ttl=$(( POLL_INTERVAL * 20 ))
    if (( prev_deficit > 0 && prev_deficit_ts > 0 )) \
        && (( now_epoch - prev_deficit_ts > _deficit_ttl )); then
        prev_deficit=0
    fi
    # LATE ARM (Codex P1, #1790). `main` calls `_oom_arm_baseline` exactly once,
    # at startup. If `memory.events` was unreadable at that moment the spec came
    # back empty -- "monitoring unavailable" -- and the journal cursor was never
    # anchored, because arming returns before it gets that far. The baseline is
    # then actually established HERE, on the first tick where the counter reads,
    # and emitting drain=0 from that path would hand the next kill's query a
    # fallback time window in which a PRE-BASELINE record can explain it away.
    # A malformed spec lands here too, and for the same reason: we do not know
    # what the cursor points at, so we re-anchor or refuse to trust it.
    if [[ -z "$prev" ]]; then
        _oom_arm_cursor || prev_drain=1
        # `main` already told the operator monitoring was off. It is not, from
        # here on, and a log that never retracts a scary line is how someone
        # concludes the monitor is dead while it is running.
        log INFO "OOM event capture armed late (baseline oom_kill=${cur}${prev_drain:+, drain=${prev_drain}})"
    fi
    if [[ -n "$prev" ]] && (( cur > prev )); then
        local n=$(( cur - prev )) stamp
        stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        {
            echo "# OOM event ${stamp}: cgroup oom_kill ${prev} -> ${cur} (+${n})"
            echo "## memory (MB):"; free -m 2>/dev/null | head -2
            echo "## top RSS:"; ps -eo pid,rss,comm --sort=-rss 2>/dev/null | head -12
            echo
        } >> "$OOM_LOG" 2>/dev/null || true
        log WARN "cgroup OOM kill detected (oom_kill ${prev} -> ${cur}); snapshot → ${OOM_LOG}"
        # ATTRIBUTE before paging (issue #1775): the root counter aggregates
        # oom_kill from every descendant cgroup, so a by-design kill inside a
        # resource-capped child scope reads identically to genuine container
        # pressure. Ask the journal which unit died; when EVERY killed unit is
        # a known contained scope, the cap did its job — record it (snapshot +
        # WARN log stay either way) and do not page. Anything else — a
        # non-contained unit, no record, or no journal — pages exactly as
        # before: attribution can only ever DOWNGRADE a known-contained kill,
        # never silence an unknown one.
        #
        # TRIGGER CHECK FIRST (Codex P1, #1790): the journal names the VICTIM,
        # not the cgroup whose limit fired. When the container root's LOCAL
        # oom counter moved, the container's own limit triggered the kill —
        # the victim can still be a contained child, so journal attribution
        # must never suppress this. LIMIT OF THE MECHANISM, stated honestly:
        # an ancestor ABOVE the container root (the host/VM slice) firing and
        # victimising a contained child is indistinguishable from a contained
        # kill at this layer and can still be suppressed — no container-side
        # signal names that trigger. Unreadable local counter = unverifiable
        # trigger = the same fail direction (page).
        local _container_trigger=0
        if [[ -n "$loc_oom" && -n "$prev_local" ]] && (( loc_oom > prev_local )); then
            _container_trigger=1
        fi
        local _oom_units="" _oom_who="unattributed" _oom_n=0 _oom_query_ok=0
        if _oom_units=$(_oom_killed_units); then
            _oom_query_ok=1
            if [[ -n "$_oom_units" ]]; then
                _oom_who=$(printf '%s\n' "$_oom_units" | sort -u | paste -sd, -)
                _oom_n=$(printf '%s\n' "$_oom_units" | grep -c . || true)
            fi
        else
            _oom_units=""
        fi
        # RECONCILE (see the spec comment): the obligations are the owed
        # deficit PLUS this tick's delta; the records returned retire them.
        # Suppress only when records FULLY account for every obligation and
        # every named unit is contained. Anything else pages, and the
        # unaccounted remainder carries forward as the new deficit — which is
        # why a LATE record (returned by a later query) can never cover a kill
        # it does not belong to: by then its own kill is already an
        # obligation. EVERY observed kill must be accounted for, not merely
        # SOME of them (the partially attributed batch was the original
        # fail-open here).
        local _obligations=$(( prev_deficit + n )) _new_deficit
        _new_deficit=$(( _obligations - _oom_n ))
        (( _new_deficit < 0 )) && _new_deficit=0
        if (( prev_drain == 0 && _container_trigger == 0 )) \
            && [[ -n "$loc_oom" && -n "$prev_local" && -n "$_oom_units" ]] \
            && (( _oom_n == _obligations )) \
            && _oom_units_all_contained "$_oom_units"; then
            log WARN "OOM kill contained in [${_oom_who}] — its own resource cap fired, not container pressure; not paging (snapshot kept)"
        else
            # Emergency tier (pages): an OOM kill is a discrete serious event —
            # the usual reason a CC session vanishing with no crash message —
            # not routine tier pressure, so unlike ORANGE it warrants a
            # proactive page (per the 2026-08-19 decision). Deduped per
            # distinct oom_kill total.
            local _why="killed unit(s): ${_oom_who}"
            if (( prev_drain == 1 )); then
                _why="journal cursor could not be armed at startup; killed unit(s): ${_oom_who}"
            elif [[ "$_container_trigger" -eq 1 ]]; then
                _why="container-level trigger (memory.events.local oom ${prev_local} -> ${loc_oom}); killed unit(s): ${_oom_who}"
            elif [[ -z "$loc_oom" || -z "$prev_local" ]]; then
                _why="trigger unverifiable (memory.events.local unreadable); killed unit(s): ${_oom_who}"
            fi
            queue_alert emergency "watchgod:oom" "cgroup OOM kill(s) detected" \
                "${n} process(es) OOM-killed in the container cgroup (oom_kill ${prev}->${cur}; ${_why}). A CC session vanishing with no crash message is often this. Snapshot: ${OOM_LOG}" \
                "watchgod:oom:${cur}"
        fi
        # The deficit clock only restarts when the deficit GROWS; retirements
        # keep the original timestamp so a shrinking deficit cannot live
        # forever by halves.
        if (( _new_deficit > prev_deficit )); then
            prev_deficit_ts=$now_epoch
        fi
        prev_deficit=$_new_deficit
        # drain clears ONLY on a successful query: the flag means "the cursor
        # was never anchored", and a FAILED resolution neither re-anchors it
        # nor returns records — clearing on failure would let the next tick's
        # fallback window offer pre-baseline records as attribution (audit
        # BLOCKER, #1790 round 2).
        # A SUCCESSFUL QUERY IS NOT AN ANCHORED CURSOR, and conflating them
        # gave back exactly what drain was added to prevent. MEASURED with the
        # cursor path unwritable: the arm correctly set drain=1, the next kill
        # paged and cleared drain, its re-anchor silently failed, and the kill
        # after that -- a genuine one whose own record was never written -- was
        # accounted for by the first kill's record, still inside the fallback
        # window, and suppressed. Two kills, one page. Clear drain only when the
        # cursor is verifiably on disk.
        # BOTH DIRECTIONS. `drain` means "the cursor is not anchored", so the
        # cursor decides it -- clearing it on success while never SETTING it left
        # the inverse open, and the inverse is reachable from the ordinary
        # drain=0 state: a re-anchor that cannot persist deletes the cursor
        # (:549) and leaves drain=0 behind, so the next query falls back to the
        # relative window and re-reads the record this tick just counted.
        # MEASURED from `4:0:0:0:0` with the cursor path unwritable: a contained
        # kill, then a real kill that wrote no record of its own -- TWO kills,
        # ZERO pages. Setting drain from the cursor closes it in one place
        # rather than at each site that can fail to write.
        # Clearing needs BOTH: the query succeeded AND the cursor on disk is
        # anchored. The anchored check alone is syntax -- a stale file from a
        # dead epoch passes it -- and on a failed query nothing re-anchored, so
        # clearing there trusts a position nobody verified. Setting needs only
        # the anchor to be missing.
        if ! _oom_cursor_is_anchored; then
            prev_drain=1
        elif (( _oom_query_ok == 1 )); then
            prev_drain=0
        fi
        # Bound the OOM log (retention discipline — matches cc_exit/log rotation);
        # keep the most recent ~1000 lines so a thrashing container can't leak it.
        local oom_lines
        oom_lines=$(wc -l < "$OOM_LOG" 2>/dev/null || echo 0)
        if (( ${oom_lines:-0} > 1000 )); then
            tail -n 1000 "$OOM_LOG" > "${OOM_LOG}.tmp" 2>/dev/null && mv "${OOM_LOG}.tmp" "$OOM_LOG" 2>/dev/null || true
        fi
    fi
    # A transient unreadable local counter must not DISARM future trigger
    # verification: carry the last known local baseline forward (the tick
    # itself still pages — the current value is unknown — and a jump observed
    # once the file is readable again correctly reads as a container trigger).
    printf '%s' "${cur}:${loc_oom:-$prev_local}:${prev_deficit}:${prev_deficit_ts}:${prev_drain}"
}

_oom_persist_cursor() {
    # $1 = a cursor value (with or without the leading `s=`). rc 0 ONLY when the
    # cursor file now verifiably holds it.
    #
    # THE SINGLE WRITER. Every path that advances the cursor goes through here,
    # because a cursor that was not persisted is the one state the whole
    # suppression mechanism cannot survive: queries fall back to a TIME WINDOW,
    # where a record written before the baseline can account for a kill that
    # happened after it.
    #
    # The write's exit status is not enough on its own -- a full filesystem
    # reports the failure on close, leaving an empty or truncated file behind --
    # so the value is read BACK and compared. At the point this matters an
    # unreadable cursor and an absent one are the same thing, and they get the
    # same answer.
    local _want="s=${1#s=}" _back=""
    [[ "$_want" != "s=" ]] || return 1
    # The write's own status is checked, and then the value is read back and
    # compared to what we MEANT to write. The comparison subsumes the status
    # check -- MEASURED: reverting this `|| return 1` to `|| true` turns no test
    # red, because a failed write leaves either nothing (read-back fails) or the
    # OLD value (read-back mismatches). It is kept as the cheap early exit and
    # because a future refactor that weakened the read-back to a bare `s=*`
    # prefix test would make it load-bearing again.
    printf '%s' "$_want" > "$_OOM_CURSOR_FILE" 2>/dev/null || return 1
    _back=$(cat "$_OOM_CURSOR_FILE" 2>/dev/null) || return 1
    [[ "$_back" == "$_want" ]] || return 1
    return 0
}

_oom_cursor_is_anchored() {
    # rc 0 when a usable cursor is on disk. This is the fact `drain` denies, so
    # nothing may clear drain without it.
    local _back=""
    _back=$(cat "$_OOM_CURSOR_FILE" 2>/dev/null) || return 1
    [[ "$_back" == s=* ]] || return 1
    return 0
}

_oom_arm_cursor() {
    # Advance the journal cursor to the current tail and PERSIST it.
    #
    # rc 0 ONLY when the cursor file now holds that position. rc 1 for every
    # other outcome -- journalctl absent, the query failing, the write failing,
    # or a write that reported success and left nothing readable behind. The
    # caller must carry drain=1 on rc 1, because an unarmed cursor is not a
    # cosmetic gap: the next query falls back to a TIME WINDOW, where a record
    # written BEFORE the baseline can account for a kill that happened after it
    # and suppress a real page.
    #
    # `-n 0 --show-cursor` prints the tail cursor with no entries, so this
    # advances the position without consuming anything.
    command -v journalctl >/dev/null 2>&1 || return 1
    local _tail_cursor=""
    _tail_cursor=$(journalctl --user -n 0 --show-cursor --no-pager -o cat 2>/dev/null \
        | sed -n 's/^-- cursor: //p' | tail -1) || _tail_cursor=""
    [[ -n "$_tail_cursor" ]] || return 1
    _oom_persist_cursor "$_tail_cursor"
}

_oom_arm_baseline() {
    # Echo the initial OOM baseline spec
    # ("counter:local_oom:deficit:deficit_ts:drain"; empty = monitoring
    # unavailable) and advance the journal cursor to the current tail (Codex
    # P1, #1790): records of kills that predate the baseline — written before
    # this (re)start, or left behind the cursor by a kill that landed in the
    # gap — must never account for a post-startup kill. `-n 0 --show-cursor`
    # prints the tail cursor with no entries, so this advances the position
    # without consuming anything. When the cursor cannot be armed (journalctl
    # absent or failing), the spec carries drain=1: the first resolution then
    # pages and re-anchors rather than trusting the fallback window's
    # pre-baseline records.
    local base="" loc="" drain=0
    base=$(_read_oom_kill) || base=""
    [[ -z "$base" ]] && { printf '%s' ""; return 0; }
    loc=$(_read_oom_local_trigger) || loc=""
    if ! _oom_arm_cursor; then
        drain=1
        # A cursor file from a PREVIOUS daemon epoch must not survive a failed
        # arm. It passes the anchored check on syntax, but its POSITION predates
        # this baseline -- a query from it returns pre-baseline records, and one
        # of those can account for a post-baseline kill. MEASURED: with a stale
        # file surviving, drain=0 and an expired deficit, one real line-less
        # kill produced ZERO pages. Absent file -> queries use the bounded
        # fallback window and drain=1 covers the first resolution.
        rm -f "$_OOM_CURSOR_FILE" 2>/dev/null || true
    fi
    printf '%s' "${base}:${loc}:0:0:${drain}"
}


# ── Main loop ────────────────────────────────────────────────
main() {
    mkdir -p "$(dirname "$LOG_FILE")" "$ALERT_DIR"
    load_config
    mkdir -p "$DG_STATE_DIR"
    # v1's shared tier flags. Nothing reads them, and v2 never clears them, so
    # a leftover would sit there forever looking like a live alarm.
    rm -f "$ALERT_DIR/tmp_warning" "$ALERT_DIR/tmp_emergency" "$ALERT_DIR/tmp_orange_stuck" 2>/dev/null || true
    log INFO "Watchgod v2 starting (poll=${POLL_INTERVAL}s, fast=${FAST_POLL_INTERVAL}s, act=${WATCHGOD_ACT}, downloads=${DOWNLOADS_DIR})"
    (( WATCHGOD_ACT )) || log WARN "OBSERVE mode (WATCHGOD_ACT=0): tiers are measured and logged; nothing is reclaimed, released, frozen or paged"

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
            tail -100 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
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
