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
#   CODE_INTEL_INDEX_MEMORY_MAX   default: measured 4096M, BOUNDED by the container
#   CODE_INTEL_INDEX_OOM_SCORE_ADJ default 900    (above cc/invoker.py's 500; raise-only)
#   CODE_INTEL_INDEX_IO_WEIGHT    default 20     (1-10000; low = polite)
#   CODE_INTEL_INDEX_CPU_QUOTA    default 200%   (2 cores worth)
#   CODE_INTEL_INDEX_MODE         default fast   (fast|moderate|full; 3rd arg wins)
#   CODE_INTEL_INDEX_PERSISTENCE  default true   (cbm .codebase-memory artifact)
#   CODE_INTEL_INDEX_LOCK_SKIP_RC default 0      (runner sets 75)
#   CODE_INTEL_INDEX_DISABLE=1    skip all indexing (escape hatch)

set -u

# Resolve HOME when unset: stripped-env/systemd/sandbox invocations can leave
# HOME unset, which under `set -u` aborts at the first ${HOME} use. Fall back
# to the passwd entry for the current uid (same source Path.home() uses); fail
# closed if unresolvable. See CC memory sandbox_shell_no_home.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "ERROR: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

REPO_PATH="${1:-}"
TOOLS="${2:-both}"
MODE="${3:-${CODE_INTEL_INDEX_MODE:-fast}}"

# Cap sized FROM A MEASUREMENT, not a guess (closes #1776).
#
# The old 2G default was ~40% BELOW what the job actually needs, so the indexer
# was killed for being correctly sized against a wrong limit. MEASURED
# 2026-09-08: a clean codebase-memory fast-index of this repo peaks at
# 2,836 MB RSS (48,986 nodes / 302,236 edges, artifact written, rc=0, run under
# a deliberately generous 8G cap). Every observed kill was
# constraint=CONSTRAINT_MEMCG inside a code-intel-* scope sitting on its own 2G
# limit at anon-rss ~2,084,000 kB — i.e. the cap was the killer, not container
# pressure and not a leak. 4G is that measurement plus ~40% headroom.
#
# Note CBM_MEM_BUDGET_MB is NOT the lever for this: pinned to 1500 the index
# still died at 2.03G, so it does not bound the index path.
#
# NOT a percentage-of-RAM cap, and that half of the reasoning stands: the
# indexer's requirement scales with the REPO being indexed, not with the host, so
# a pure 25%-of-RAM cap would be 8G here and 2G on an 8 GiB box — REPRODUCING
# this exact bug on small installs. The TARGET must come from the measurement.
#
# A percentage would also be actively unsafe here: the rlimit fallback below
# parses only <int>[.frac]G|M, so "25%" falls through to "running
# memory-uncapped" — failing OPEN to something worse than the bug.
# (Percentages DO work on a systemd scope, e.g. cc/invoker.py — but only on the
# scope path, and the fallback is the trap.)
#
# .claude/mcp/run-codebase-memory keeps 2G on purpose; see the matching comment
# there. It caps a long-lived SERVER against an upstream leak, not a batch job
# whose size is set by the repo. The divergence is a decision, not drift.
#
# BUT the measured floor alone is not a safe cap, and shipping it as an absolute
# 4G was wrong: scripts/host-setup.sh floors a Genesis install at 4 GiB, so on a
# MINIMUM install MemoryMax=4G EQUALS the container limit and the scope stops
# isolating anything — the parent cgroup reaches its own OOM before the scope
# boundary is ever hit, taking the server or a session with it. A cap equal to
# the whole container is not a cap.
#
# So: the measured need is the TARGET, and the container's real limit BOUNDS it.
# That keeps the anti-percentage argument above intact (the target still comes
# from a measurement, not from a fraction of whatever host we land on) while
# guaranteeing the scope can actually fire before the container does.
# _derive_mem_max emits an explicit <N>M value, never a percentage, so the rlimit
# fallback below can still parse it.
#
# On a box too small for the bounded cap the index will still be killed at its
# scope — that is the CORRECT failure: it protects the container instead of
# taking it down. The real answer for such a host is not to start the job at all,
# which is admission control and is deliberately NOT claimed here as present.
_CI_MEM_TARGET_MB=4096   # the 2,836 MB measurement + ~40% headroom
_CI_MEM_CONTAINER_FRACTION=60  # percent of the container limit the cap may take
# ...AND never come within this much of the container limit. The fraction alone
# is not enough: 60% of a 4 GiB minimum install is 2,457M, which leaves 1,639M
# for genesis-server + Qdrant + a CC session together — so the parent cgroup can
# still reach its own OOM before the scope boundary, which is the failure the
# bound exists to prevent. A reserve states the invariant directly ("always leave
# this much for everything else") instead of hoping a percentage happens to.
# 2 GiB is the rough floor for server + Qdrant + one session on this reference
# install; it is not a measurement of a minimum install, and is deliberately
# conservative because being wrong small costs a stale index, while being wrong
# large costs the container.
_CI_MEM_RESERVE_MB=2048

# What this CANNOT do, stated so nobody reads more into it: no static cap can
# guarantee the scope fires before the container, because the headroom at the
# moment of pressure depends on what everything else is doing. That guarantee
# needs admission control — refusing to START a job that cannot fit — which is
# NOT built (issue tracked separately) and is NOT claimed here. What the bound
# does deliver is that the cap can never approach the container limit, and the
# oom_score_adj below makes the indexer the preferred victim if the container
# does hit its own OOM.
_derive_mem_max() {
    # Container limit from cgroup v2, then v1. "max" (uncapped) or unreadable
    # means nothing bounds us, so the measured target stands.
    #
    # Read with the `read` BUILTIN, not `cat`: this entrypoint can run with a
    # minimal PATH (the rlimit-fallback environment its own tests construct), and
    # `cat` missing there made the read fail, empty the value, and silently
    # return the UNBOUNDED target — restoring a cap equal to the parent limit on
    # exactly the constrained install the bound protects. A builtin cannot go
    # missing. Failure now `continue`s to the next candidate instead of breaking
    # out of the loop, so an unreadable v2 path still lets v1 be tried.
    local limit_bytes="" f
    for f in "${CODE_INTEL_FAKE_CGROUP_LIMIT_FILE:-/sys/fs/cgroup/memory.max}" \
             /sys/fs/cgroup/memory/memory.limit_in_bytes; do
        [ -r "$f" ] || continue
        # Do NOT gate on read's exit status: `read` returns non-zero at EOF when
        # the file has no trailing newline, even though it HAS set the variable.
        # Gating on it made a newline-less memory.max look unreadable and fall
        # through to the unbounded target — the exact fail-open this bound
        # exists to prevent. The value being non-empty is the real success test.
        read -r limit_bytes < "$f" 2>/dev/null || true
        [ -n "$limit_bytes" ] && break
        limit_bytes=""
    done
    case "$limit_bytes" in
        '' | max | *[!0-9]*)
            printf '%sM\n' "$_CI_MEM_TARGET_MB"
            return 0
            ;;
    esac
    local limit_mb=$(( limit_bytes / 1024 / 1024 ))
    # An implausibly huge v1 "no limit" sentinel behaves like uncapped.
    if [ "$limit_mb" -le 0 ] || [ "$limit_mb" -gt 4194304 ]; then
        printf '%sM\n' "$_CI_MEM_TARGET_MB"
        return 0
    fi

    # The cap is the SMALLEST of: the measured target, the container fraction,
    # and whatever is left after the reserve.
    local cap=$_CI_MEM_TARGET_MB
    local by_fraction=$(( limit_mb * _CI_MEM_CONTAINER_FRACTION / 100 ))
    [ "$by_fraction" -lt "$cap" ] && cap=$by_fraction
    local by_reserve=$(( limit_mb - _CI_MEM_RESERVE_MB ))
    [ "$by_reserve" -lt "$cap" ] && cap=$by_reserve

    # A container smaller than the reserve makes by_reserve <= 0. Emit a small
    # positive floor rather than "0M" or a negative: systemd would reject the
    # malformed value and the scope would carry NO cap at all, which is the
    # fail-open-to-worse this whole block exists to avoid. Such a host cannot
    # run an index that needs 2.8 GiB regardless — it will be killed at its
    # scope, which is the correct failure (the container survives).
    [ "$cap" -lt 256 ] && cap=256
    printf '%sM\n' "$cap"
}

MEM_MAX="${CODE_INTEL_INDEX_MEMORY_MAX:-$(_derive_mem_max)}"
IO_WEIGHT="${CODE_INTEL_INDEX_IO_WEIGHT:-20}"
CPU_QUOTA="${CODE_INTEL_INDEX_CPU_QUOTA:-200%}"
PERSISTENCE="${CODE_INTEL_INDEX_PERSISTENCE:-true}"

_log() { printf '[code-intel-index] %s\n' "$*"; }

# Make this job the kernel's PREFERRED victim under CONTAINER-wide memory
# pressure — the safety counterpart of the larger cap above. A bigger cap means
# a bigger consumer, and the thing that must never be killed instead of this one
# is a CC session holding a user's in-flight work.
#
# Mechanics, all MEASURED rather than assumed:
#  * CORRECTION (MEASURED 2026-09-09, replacing what this comment said before):
#    lowering oom_score_adj is NOT refused. The only constraint is
#    oom_score_adj_min, which is 0 here (inherited from init) — so an unprivileged
#    task may set ANY value in 0..1000, in either direction, and only a NEGATIVE
#    value is refused. Verified directly: 500 -> 321 accepted, 321 -> 0 accepted,
#    -1 refused. The earlier "raising works, lowering never does" was wrong, and
#    it mattered: an unconditional write would silently LOWER an inherited higher
#    value, making this job LESS likely to be chosen than its parent intended.
#    The raise-only contract therefore has to be enforced here, in code, because
#    the kernel does not enforce it. See the inherited-value check below.
#  * `-p OOMScoreAdjust=` is INVALID on `systemd-run --scope` ("Unknown
#    assignment") because a scope does not exec, so Exec properties do not
#    apply. A self-write is the mechanism that works while KEEPING --scope,
#    which is load-bearing here (it keeps the job a child of this script so the
#    watchdog's pgid isolation and stdio survive).
#  * The value is INHERITED across fork/exec and survives
#    `systemd-run --user --scope` (verified: child reads 500), and systemd does
#    not reset it for a scope, so writing it once here covers the indexer.
#  * On the 32 GiB reference host each 100 of adj is worth ~3.2 GB of
#    oom_badness, so these rungs decide outcomes rather than express a taste.
#
# 900, NOT 500 — and this is the correction that makes the whole feature work.
# 500 was chosen without checking what already uses it, and `cc/invoker.py`
# ALREADY assigns every CC subprocess exactly 500 (its `set_oom_score_adj`
# default, applied at both call sites). At EQUAL adj the kernel falls back to
# each process's memory charge, and a CC session may be allowed far more memory
# than this job — so "the index dies before a session" would have been false in
# precisely the container-wide pressure it was written for. A distinctly higher
# value is what makes the ordering real. 1000 is deliberately left unused as the
# ceiling; nothing here needs to outrank a batch index that can simply re-run.
#
# Values are normalised to CANONICAL DECIMAL before the write. MEASURED: the
# kernel parses this file with base autodetection, so a zero-padded "0500" is
# read as OCTAL and applies 320 — silently WEAKENING the preference while the
# log would have echoed the operator's "0500" back as if it took. Range is
# checked too: the kernel's valid band is -1000..1000 and only non-negative
# values are achievable here (lowering is refused), so anything above 1000 or
# non-numeric is rejected at the lever rather than written and misapplied.
_apply_oom_score_adj() {
    # Raise-only guards the DEFAULT, not an operator's explicit instruction —
    # that distinction is the whole point. The risk being fixed is this script's
    # own 900 silently undoing a parent that deliberately raised the job higher.
    # Someone who exports CODE_INTEL_INDEX_OOM_SCORE_ADJ has stated an intent,
    # and quietly ignoring a lever because it happens to lower the value would
    # be its own surprise — the lever exists to be obeyed. So: explicit wins and
    # says what it did; the default defers to a higher inherited value.
    local want explicit=0
    if [ -n "${CODE_INTEL_INDEX_OOM_SCORE_ADJ:-}" ]; then
        want="$CODE_INTEL_INDEX_OOM_SCORE_ADJ"
        explicit=1
    else
        want=900
    fi
    case "$want" in
        '' | *[!0-9]*)
            _log "WARNING: ignoring non-numeric CODE_INTEL_INDEX_OOM_SCORE_ADJ='$want' — kill order unchanged"
            return 0
            ;;
    esac

    # Reject oversized input BEFORE any arithmetic. All-digit is not the same as
    # in-range: $((10#$want)) on a value past bash's signed 64-bit range WRAPS,
    # so "18446744073709551616" evaluates to 0, passes a `> 1000` check, and is
    # written and logged as accepted. Strip leading zeros textually, then bound by
    # LENGTH first — a decimal string of more than 4 digits cannot be <= 1000, and
    # a length test cannot overflow.
    local digits="${want#"${want%%[!0]*}"}"   # drop leading zeros
    [ -n "$digits" ] || digits=0
    if [ "${#digits}" -gt 4 ]; then
        _log "WARNING: CODE_INTEL_INDEX_OOM_SCORE_ADJ='$want' exceeds the kernel maximum of 1000 — kill order unchanged"
        return 0
    fi
    # 10# forces base-10 so "0500" means five hundred, not octal 320 — the kernel
    # parses this file with base autodetection, so a zero-padded value would
    # silently apply a WEAKER preference while the log echoed the operator's
    # string back as though it took.
    local canonical=$((10#$digits))
    if [ "$canonical" -gt 1000 ]; then
        _log "WARNING: CODE_INTEL_INDEX_OOM_SCORE_ADJ='$want' exceeds the kernel maximum of 1000 — kill order unchanged"
        return 0
    fi

    # Raise-only, enforced HERE because the kernel does not enforce it (see the
    # correction above). If we inherited a value at or above what we want, keep
    # it: a parent that raised us to 1000 wanted this job even more killable than
    # our default does, and writing 900 over it would quietly reverse that.
    local current=""
    read -r current < /proc/self/oom_score_adj 2>/dev/null || current=""
    case "$current" in
        '' | *[!0-9]*) current="" ;;   # unreadable or negative — no opinion
    esac
    if [ "$explicit" = "0" ] && [ -n "$current" ] && [ "$current" -ge "$canonical" ]; then
        _log "oom_score_adj=$current inherited (>= the default $canonical) — kept, raise-only"
        return 0
    fi
    if [ "$explicit" = "1" ] && [ -n "$current" ] && [ "$current" -gt "$canonical" ]; then
        # Say it out loud. An explicit lever that lowers the inherited preference
        # is honoured, but it is also the kind of thing someone wants to see in a
        # log when they are working out why a kill went the way it did.
        _log "note: CODE_INTEL_INDEX_OOM_SCORE_ADJ=$canonical LOWERS the inherited $current (explicit override wins over raise-only)"
    fi

    if printf '%s\n' "$canonical" > /proc/self/oom_score_adj 2>/dev/null; then
        # Deliberately does NOT name other units' scores. An earlier version said
        # "the server at 100", which no shipped configuration set — the unit
        # template declared -500 (a value the user manager silently refuses, which
        # is a separate fix). Quoting a number this script does not own turns an
        # operational log line into a false claim about kill ordering.
        _log "oom_score_adj=$canonical — this job is the preferred OOM victim, ahead of CC subprocesses, the server and the session"
    else
        # Not fatal: an unwritable /proc (odd sandbox) costs kill-order
        # preference, never the index itself.
        _log "WARNING: could not raise oom_score_adj — kill order unchanged"
    fi
}

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
# Raise the kill-order preference BEFORE the probe, so every descendant this
# script goes on to create — probe, scope, indexer — inherits it. Doing it after
# the probe would leave the first scope at the default.
_apply_oom_score_adj

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
