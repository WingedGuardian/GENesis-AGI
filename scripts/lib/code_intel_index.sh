#!/usr/bin/env bash
# code_intel_index.sh — the ONE entrypoint for code-intelligence indexing.
#
#   Usage: code_intel_index.sh <repo_path> [cbm|gitnexus|both] [fast|moderate|full]
#
# Every code-intel index spawn (codebase-memory-mcp cli index_repository,
# gitnexus analyze) MUST go through this script. Raw spawns caused a
# container-wedging incident: three concurrent full-repo indexers on one
# worktree saturated the container's disk-write throttle, piling every
# writer into D-state (load ~58 at 79% memory) and hanging sshd/incus-exec.
# A guardrail test (test_code_intel_index.py) fails any new raw spawn site.
#
# What this enforces, in order:
#   1. WORKTREE SKIP — linked git worktrees (.git is a file) are never
#      indexed: their code is ~identical to the main repo, each index costs
#      a full separate graph (~GBs + heavy writes), and Serena (live LSP)
#      covers worktree sessions without any index.
#   2. SINGLE-FLIGHT — a non-blocking flock per repo path: if an index for
#      this repo is already running, exit 0 immediately (never queue).
#   3. RESOURCE CAPS — both tools run inside a systemd user scope with
#      MemoryMax / MemorySwapMax=0 / IOWeight / CPUQuota, so even a single
#      index cannot starve the container. Where no systemd user manager is
#      reachable (CI, containers without a user bus), falls back to
#      nice/ionice + a soft address-space rlimit.
#
# The script runs the requested tools SEQUENTIALLY in the foreground and
# exits when done — but it is NOT meant to be called on every commit anymore.
# Triggers write an index-request marker (scripts/lib/index_marker.py) and the
# idle-gated runner (scripts/code_intel_runner.sh) is the only caller that
# consumes a marker and invokes this entrypoint, when the box is idle. A
# fire-and-forget spawn here was the second index-storm's trigger.
#
# MODE (3rd arg, default fast): fast = filtered files, no similarity/semantic
# edges — cheap, the routine default. full = all files + similarity/semantic,
# the expensive pipeline that saturated the container's read throttle; reserve
# it for the runner's weekly-full window. GitNexus is incremental regardless.
#
# Exit codes: 0 success · 1 hard error · 3 a REQUESTED tool was missing from
# PATH (nothing indexed — the runner must NOT treat this as success, or a
# minimal-PATH service unit silently disables indexing forever) · the lock-skip
# path returns CODE_INTEL_INDEX_LOCK_SKIP_RC (default 0; the runner sets 75 to
# tell "lock held / host-frozen — keep the marker" apart from a real success).
#
# Env overrides:
#   CODE_INTEL_INDEX_MEMORY_MAX   default 2G     (per systemd scope)
#   CODE_INTEL_INDEX_IO_WEIGHT    default 20     (1-10000; low = polite)
#   CODE_INTEL_INDEX_CPU_QUOTA    default 200%   (2 cores worth)
#   CODE_INTEL_INDEX_MODE         default fast   (fast|moderate|full; 3rd arg wins)
#   CODE_INTEL_INDEX_PERSISTENCE  default true   (cbm .codebase-memory artifact)
#   CODE_INTEL_INDEX_LOCK_SKIP_RC default 0      (runner sets 75)
#   CODE_INTEL_INDEX_DISABLE=1    skip all indexing (escape hatch)

set -u

REPO_PATH="${1:-}"
TOOLS="${2:-both}"
MODE="${3:-${CODE_INTEL_INDEX_MODE:-fast}}"

MEM_MAX="${CODE_INTEL_INDEX_MEMORY_MAX:-2G}"
IO_WEIGHT="${CODE_INTEL_INDEX_IO_WEIGHT:-20}"
CPU_QUOTA="${CODE_INTEL_INDEX_CPU_QUOTA:-200%}"
PERSISTENCE="${CODE_INTEL_INDEX_PERSISTENCE:-true}"

_log() { printf '[code-intel-index] %s\n' "$*"; }

# Shared load/iowait sampler for the pressure watchdog. Best-effort: if it's
# missing (older checkout), the watchdog degrades to a wall-clock cap only.
_PROC_PRESSURE="$(dirname "${BASH_SOURCE[0]}")/proc_pressure.sh"
# shellcheck source=proc_pressure.sh
[ -f "$_PROC_PRESSURE" ] && . "$_PROC_PRESSURE"

# Pressure-watchdog knobs (env-overridable for tests). The watchdog is the ONLY
# I/O throttle that works on this host: ionice is inert (disk scheduler none)
# and IOWeight is inert in user scopes (no io controller delegated), so a full
# index otherwise reads at the container's entire throttle and storms it. It
# pauses the index (cgroup freeze, or SIGSTOP on the fallback path) under
# pressure and kills a run that can never make headway.
_WD_INTERVAL="${CODE_INTEL_WATCHDOG_INTERVAL:-15}"        # steady-state sample gap (s)
_WD_WARMUP_INTERVAL="${CODE_INTEL_WATCHDOG_WARMUP_INTERVAL:-5}"  # tighter during the burst window
_WD_WARMUP_S="${CODE_INTEL_WATCHDOG_WARMUP_S:-30}"        # freeze-on-1-bad for the first N s
_WD_LOAD_MAX="${CODE_INTEL_WATCHDOG_LOAD_MAX:-4}"         # loadavg1 above this = pressure
_WD_IOWAIT_MAX="${CODE_INTEL_WATCHDOG_IOWAIT_MAX:-25}"    # iowait% above this = pressure
_WD_BAD_SAMPLES="${CODE_INTEL_WATCHDOG_BAD_SAMPLES:-2}"   # consecutive bad samples before pause
_WD_CONT_PAUSE_MAX="${CODE_INTEL_WATCHDOG_CONT_PAUSE_MAX:-600}"  # kill if paused this long CONTINUOUSLY
_WD_WALL_FAST="${CODE_INTEL_WATCHDOG_WALL_FAST:-3600}"    # wall cap for fast/moderate (s)
_WD_WALL_FULL="${CODE_INTEL_WATCHDOG_WALL_FULL:-14400}"   # wall cap for full (s) — cbm can't resume

if [ "${CODE_INTEL_INDEX_DISABLE:-0}" = "1" ]; then
    _log "disabled via CODE_INTEL_INDEX_DISABLE — skipping"
    exit 0
fi

if [ -z "$REPO_PATH" ] || [ ! -d "$REPO_PATH" ]; then
    _log "ERROR: repo path missing or not a directory: '$REPO_PATH'"
    exit 1
fi
case "$TOOLS" in cbm|gitnexus|both) ;; *)
    _log "ERROR: tool must be cbm|gitnexus|both, got '$TOOLS'"; exit 1 ;;
esac
case "$MODE" in fast|moderate|full) ;; *)
    _log "ERROR: mode must be fast|moderate|full, got '$MODE'"; exit 1 ;;
esac

# Physical path (-P): the single-flight lock is keyed on this, and a symlinked
# spelling of the same repo must not get a second lock (= second concurrent index).
REPO_PATH="$(cd "$REPO_PATH" && pwd -P)"

# ── 1. Worktree skip ────────────────────────────────────────────────────
# In a linked worktree, <root>/.git is a FILE (gitdir pointer), not a dir.
if [ -f "$REPO_PATH/.git" ]; then
    _log "skip: $REPO_PATH is a linked git worktree (never indexed — use Serena there)"
    exit 0
fi

# ── 2. Single-flight lock (per repo path) ───────────────────────────────
LOCK_DIR="${GENESIS_HOME:-$HOME/.genesis}/locks"
mkdir -p "$LOCK_DIR" 2>/dev/null || LOCK_DIR="${TMPDIR:-/tmp}"
LOCK_FILE="$LOCK_DIR/code-intel-$(printf '%s' "$REPO_PATH" | sha1sum | cut -c1-16).lock"

# Take the lock only when flock AND a writable lock file are both available.
# If either is missing, proceed UNLOCKED with a warning — a missing lock tool
# must degrade to "no dedup", never to "silently skip indexing" (a bare
# `flock -n 9` failure is indistinguishable from "lock held" otherwise).
if command -v flock >/dev/null 2>&1 && { exec 9>"$LOCK_FILE"; } 2>/dev/null; then
    if ! flock -n 9; then
        # Lock held: either a concurrent index, or the managed genesis-code-intel-freeze
        # user unit (scripts/code_intel_freeze.sh) holding THIS flock as a kill-switch.
        # Default rc 0 (back-compat); the runner sets CODE_INTEL_INDEX_LOCK_SKIP_RC=75
        # so it can tell "frozen — keep the marker" apart from a real success. The lock
        # ACQUISITION above is byte-unchanged, so the freeze keeps neutralizing every
        # trigger regardless.
        _log "skip: an index for $REPO_PATH is already running (lock held)"
        exit "${CODE_INTEL_INDEX_LOCK_SKIP_RC:-0}"
    fi
else
    _log "WARNING: flock or lock file unavailable ($LOCK_FILE) — proceeding UNLOCKED"
fi

# ── 3. Resource-capped runner ───────────────────────────────────────────
# Probe systemd-run exactly like .claude/mcp/run-codebase-memory does: the
# probe must create a real scope, because CC-spawned / hook-spawned contexts
# sometimes cannot reach the user manager even when systemd-run exists.
_SCOPE_OK=0
if command -v systemd-run >/dev/null 2>&1; then
    if systemd-run --user --scope --quiet \
        -p "MemoryMax=${MEM_MAX}" -p "MemorySwapMax=0" \
        -p "IOWeight=${IO_WEIGHT}" -p "CPUQuota=${CPU_QUOTA}" \
        -- /bin/true 2>/dev/null; then
        _SCOPE_OK=1
    fi
fi

_run_capped() {
    if [ "$_SCOPE_OK" = "1" ]; then
        # _CI_SCOPE_UNIT (set by _run_with_watchdog) gives the scope a
        # deterministic name so the watchdog can freeze/thaw/stop it by unit.
        systemd-run --user --scope --quiet \
            ${_CI_SCOPE_UNIT:+--unit="$_CI_SCOPE_UNIT"} \
            -p "MemoryMax=${MEM_MAX}" -p "MemorySwapMax=0" \
            -p "IOWeight=${IO_WEIGHT}" -p "CPUQuota=${CPU_QUOTA}" \
            --description "code-intel index: $REPO_PATH" \
            -- "$@"
    else
        # Fallback: polite scheduling + soft address-space cap. Mirrors the
        # run-codebase-memory launcher's degradation (never block on missing
        # systemd — CI and minimal containers must still work).
        local mem_kb=""
        if [[ "$MEM_MAX" =~ ^([0-9]+)(\.[0-9]+)?([Gg])$ ]]; then
            mem_kb=$(( ${BASH_REMATCH[1]} * 1024 * 1024 ))
        elif [[ "$MEM_MAX" =~ ^([0-9]+)(\.[0-9]+)?([Mm])$ ]]; then
            mem_kb=$(( ${BASH_REMATCH[1]} * 1024 ))
        else
            _log "WARNING: cannot parse '$MEM_MAX' for the rlimit fallback — running memory-uncapped (nice/ionice only)"
        fi
        (
            [ -n "$mem_kb" ] && ulimit -v "$mem_kb" 2>/dev/null
            if command -v ionice >/dev/null 2>&1; then
                exec nice -n 19 ionice -c 3 "$@"
            else
                exec nice -n 19 "$@"
            fi
        )
    fi
}

# ── Pressure watchdog ────────────────────────────────────────────────────
_wall_cap() { case "$MODE" in full) printf '%s' "$_WD_WALL_FULL" ;; *) printf '%s' "$_WD_WALL_FAST" ;; esac; }

# Pause / resume / kill primitives. kind=scope -> cgroup freeze the named scope
# (stops ALL descendants, escape-proof, the only working throttle here);
# kind=pgid -> SIGSTOP/CONT/KILL the isolated process group; kind=nosig ->
# can't pause, only wall-cap kill of the single job pid.
_wd_pause()  { case "$1" in scope) systemctl --user freeze "$2.scope" 2>/dev/null ;; pgid) kill -STOP -- "-$2" 2>/dev/null ;; esac; }
_wd_resume() { case "$1" in scope) systemctl --user thaw   "$2.scope" 2>/dev/null ;; pgid) kill -CONT -- "-$2" 2>/dev/null ;; esac; }
_wd_kill()   {
    case "$1" in
        scope) systemctl --user stop "$2.scope" 2>/dev/null ;;
        pgid)  kill -CONT -- "-$2" 2>/dev/null; kill -KILL -- "-$2" 2>/dev/null ;;
        *)     kill -KILL "$3" 2>/dev/null ;;
    esac
}

# _watchdog <kind> <target> <job_pid>: babysit a running index; pause under
# pressure, resume when calm, kill a run that can't make headway.
_watchdog() {
    local kind="$1" target="$2" job_pid="$3"
    local wall start now bad=0 paused=0 pause_start=0
    wall="$(_wall_cap)"; start="$(date +%s)"
    local have_sampler=1
    command -v pressure_loadavg1 >/dev/null 2>&1 || have_sampler=0
    while kill -0 "$job_pid" 2>/dev/null; do
        now="$(date +%s)"
        if [ "$(( now - start ))" -ge "$wall" ]; then
            _log "watchdog: wall cap ${wall}s reached (mode=$MODE) — killing index"
            _wd_kill "$kind" "$target" "$job_pid"; return 0
        fi
        if [ "$paused" = 1 ] && [ "$(( now - pause_start ))" -ge "$_WD_CONT_PAUSE_MAX" ]; then
            _log "watchdog: paused ${_WD_CONT_PAUSE_MAX}s continuously (system never calmed) — killing index"
            _wd_kill "$kind" "$target" "$job_pid"; return 0
        fi
        local interval="$_WD_INTERVAL" need="$_WD_BAD_SAMPLES"
        if [ "$(( now - start ))" -lt "$_WD_WARMUP_S" ]; then
            interval="$_WD_WARMUP_INTERVAL"; need=1   # burst window: freeze on first bad sample
        fi
        if [ "$have_sampler" = 1 ] && [ "$kind" != "nosig" ]; then
            local load iow
            load="$(pressure_loadavg1)"; iow="$(pressure_iowait_pct)"
            if pressure_gt "$load" "$_WD_LOAD_MAX" || pressure_gt "$iow" "$_WD_IOWAIT_MAX"; then
                bad="$(( bad + 1 ))"
                if [ "$bad" -ge "$need" ] && [ "$paused" = 0 ]; then
                    _log "watchdog: pressure (load=$load iowait=$iow%) — pausing index"
                    _wd_pause "$kind" "$target"; paused=1; pause_start="$now"
                fi
            else
                bad=0
                if [ "$paused" = 1 ]; then
                    _log "watchdog: calm (load=$load iowait=$iow%) — resuming index"
                    _wd_resume "$kind" "$target"; paused=0
                fi
            fi
        fi
        sleep "$interval"
    done
    return 0
}

# _run_with_watchdog <tool_label> <command...>: run one tool under the watchdog.
_run_with_watchdog() {
    local label="$1"; shift
    if [ "$_SCOPE_OK" = "1" ]; then
        local unit; unit="code-intel-$(printf '%s' "$REPO_PATH" | sha1sum | cut -c1-12)-${label}-$$"
        _CI_SCOPE_UNIT="$unit" _run_capped "$@" &
        local job_pid=$!
        _watchdog scope "$unit" "$job_pid"
        wait "$job_pid"; return $?
    fi
    # Fallback (no systemd scope): isolate a process group so SIGSTOP/KILL can
    # target the whole tool subtree — NEVER our own group (that would freeze the
    # watchdog itself and never thaw). If job control didn't isolate it, degrade
    # to wall-cap-only (kind=nosig) rather than risk signalling ourselves.
    set -m 2>/dev/null || true
    _run_capped "$@" &
    local job_pid=$!
    set +m 2>/dev/null || true
    local pgid self_pgid
    pgid="$(ps -o pgid= -p "$job_pid" 2>/dev/null | tr -d ' ')"
    self_pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')"
    if [[ "$pgid" =~ ^[0-9]+$ ]] && [ "$pgid" -gt 1 ] && [ "$pgid" != "$self_pgid" ]; then
        _watchdog pgid "$pgid" "$job_pid"
    else
        _log "watchdog: could not isolate a process group (pgid='$pgid' self='$self_pgid') — wall-cap only"
        _watchdog nosig "" "$job_pid"
    fi
    wait "$job_pid"; return $?
}

RC=0
MISSING=""  # requested-but-absent tools — makes a no-op run rc=3, not a false success

if [ "$TOOLS" = "cbm" ] || [ "$TOOLS" = "both" ]; then
    if command -v codebase-memory-mcp >/dev/null 2>&1; then
        _log "indexing (codebase-memory-mcp, mode=$MODE): $REPO_PATH"
        # Flag form (cbm >=0.9): --mode selects the pipeline depth (default here is
        # fast — no similarity/semantic edges); --persistence writes the shareable
        # .codebase-memory/graph.db.zst artifact so a wiped cache restores from it
        # instead of a full 0->100 re-index.
        _run_with_watchdog cbm codebase-memory-mcp cli index_repository \
            --repo-path "$REPO_PATH" --mode "$MODE" --persistence "$PERSISTENCE" || RC=$?
    else
        _log "codebase-memory-mcp not on PATH — skipped"
        MISSING="${MISSING}cbm "
    fi
fi

if [ "$TOOLS" = "gitnexus" ] || [ "$TOOLS" = "both" ]; then
    _GN=""
    if command -v gitnexus >/dev/null 2>&1; then
        _GN="gitnexus"
    elif command -v npx >/dev/null 2>&1; then
        _GN="npx gitnexus"
    fi
    if [ -n "$_GN" ]; then
        # gitnexus analyze is already incremental (only -f forces a full re-parse)
        # and quiet by default (-v opts into verbose), so no mode plumbing here.
        # NOTE: the `--quiet` flag added in #910 does NOT exist in gitnexus 1.6.x
        # ("error: unknown option '--quiet'" -> rc 1 on EVERY run); it silently
        # broke every entrypoint-driven gitnexus index since #910. Dropped.
        _log "indexing (gitnexus analyze): $REPO_PATH"
        ( cd "$REPO_PATH" && _run_with_watchdog gitnexus $_GN analyze ) || RC=$?
    else
        _log "gitnexus not available — skipped"
        MISSING="${MISSING}gitnexus "
    fi
fi

# B1: a requested tool absent from PATH means NOTHING was indexed for it. Never
# report that as success (rc 0) — the idle runner would consume the marker and
# stamp a fresh full-index timestamp, silently disabling indexing until someone
# notices the graph is stale. Distinct rc 3 == "requested tool missing".
if [ "$RC" = "0" ] && [ -n "$MISSING" ]; then
    _log "ERROR: requested tool(s) missing from PATH: ${MISSING%% } — nothing indexed (rc=3)"
    RC=3
fi

_log "done (rc=$RC): $REPO_PATH"
exit "$RC"
