#!/usr/bin/env bash
# code_intel_runner.sh — idle-gated consumer of code-intel index requests.
#
# Run every ~5 min by genesis-code-intel.timer. Triggers (post-commit hook,
# setup, the gitnexus surplus job, disk_reclaim) no longer spawn indexers; they
# drop a marker via scripts/lib/index_marker.py. THIS runner is the only thing
# that consumes a marker and invokes the locked+capped entrypoint — and only
# when the box is idle, so a reindex can never storm the container the way the
# per-commit fire-and-forget spawns did (host load 136, 70 D-state procs).
#
# Flow per pending marker:
#   idle gate (loadavg1 < 2, iowait < 10%, no CC session > 50% CPU; relaxed
#   after 24h so work is never starved forever) -> move-aside claim -> run the
#   entrypoint FOREGROUND with CODE_INTEL_INDEX_LOCK_SKIP_RC=75 -> act on rc:
#     0  -> consume (drop the in-flight copy); stamp .last-full if it ran full
#     75 -> lock held / host-frozen: restore the marker untouched (freeze-safe)
#     3  -> a requested tool was missing: restore, NO attempts penalty, loud log
#     *  -> failure: if this was an ESCALATED full (marker mode was fast), fall
#           back to fast + back off full (no attempts penalty); otherwise
#           restore with attempts+1 (euthanized to .failed.json at the cap).
#
# Escalation: a fast marker whose graph has no recent full index runs as full
# (weekly refresh / first build), unless a recent full FAILED (backoff). cbm 0.9
# cannot resume a killed full, so the INITIAL from-scratch full rebuild is done
# SUPERVISED by an operator, not left to this loop (see the incident handoff).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# CODE_INTEL_ENTRYPOINT is a test seam (inject a fake that returns a chosen rc);
# it defaults to the real locked+capped entrypoint.
ENTRYPOINT="${CODE_INTEL_ENTRYPOINT:-$SCRIPT_DIR/lib/code_intel_index.sh}"
MARKER_PY="$SCRIPT_DIR/lib/index_marker.py"
# shellcheck source=lib/proc_pressure.sh
. "$SCRIPT_DIR/lib/proc_pressure.sh"

GENESIS_HOME="${GENESIS_HOME:-$HOME/.genesis}"
LOCK_DIR="$GENESIS_HOME/locks"
LOG_FILE="$GENESIS_HOME/code-intelligence-runner.log"

# Idle-gate thresholds (env-overridable for tests / tuning).
IDLE_LOAD="${CODE_INTEL_RUNNER_IDLE_LOAD:-2}"
IDLE_IOWAIT="${CODE_INTEL_RUNNER_IDLE_IOWAIT:-10}"
IDLE_CLAUDE_CPU="${CODE_INTEL_RUNNER_IDLE_CLAUDE_CPU:-50}"
# After this long a marker is "starved"; relax the gate to a least-bad window.
RELAX_AFTER_S="${CODE_INTEL_RUNNER_RELAX_AFTER_S:-86400}"
RELAX_LOAD="${CODE_INTEL_RUNNER_RELAX_LOAD:-6}"
RELAX_IOWAIT="${CODE_INTEL_RUNNER_RELAX_IOWAIT:-20}"

_log() {
    mkdir -p "$GENESIS_HOME" 2>/dev/null || true
    printf '%s [code-intel-runner] %s\n' \
        "$(date -Iseconds 2>/dev/null || date)" "$*" >> "$LOG_FILE" 2>&1
}

_marker() { python3 "$MARKER_PY" "$@"; }

# Returns 0 (idle enough to run) or 1. Relaxed gate once a marker is starved.
_idle_ok() {
    local age_s="$1" load iowait claude_cpu load_max iowait_max
    load="$(pressure_loadavg1)"
    iowait="$(pressure_iowait_pct)"
    if [ "$age_s" -ge "$RELAX_AFTER_S" ]; then
        load_max="$RELAX_LOAD"; iowait_max="$RELAX_IOWAIT"
        if pressure_gt "$load" "$load_max" || pressure_gt "$iowait" "$iowait_max"; then
            _log "starved marker still not idle (load=$load iowait=$iowait%, relaxed) — deferring"
            return 1
        fi
        return 0
    fi
    load_max="$IDLE_LOAD"; iowait_max="$IDLE_IOWAIT"
    if pressure_gt "$load" "$load_max" || pressure_gt "$iowait" "$iowait_max"; then
        _log "not idle (load=$load iowait=$iowait%) — deferring"
        return 1
    fi
    claude_cpu="$(pressure_max_claude_cpu)"
    if pressure_gt "$claude_cpu" "$IDLE_CLAUDE_CPU"; then
        _log "CC session busy (max claude cpu=${claude_cpu}%) — deferring"
        return 1
    fi
    return 0
}

# Runner self-lock: one tick at a time (own lock, NOT the entrypoint's).
# Fallback stays under $HOME so it works under the service's ProtectSystem=strict
# + ReadWritePaths=%h (and honors the "~/tmp, never /tmp" convention).
mkdir -p "$LOCK_DIR" 2>/dev/null || { mkdir -p "$HOME/tmp" 2>/dev/null; LOCK_DIR="$HOME/tmp"; }
RUNNER_LOCK="$LOCK_DIR/code-intel-runner.lock"
if command -v flock >/dev/null 2>&1 && { exec 8>"$RUNNER_LOCK"; } 2>/dev/null; then
    if ! flock -n 8; then
        _log "another runner tick is in progress — exiting"
        exit 0
    fi
fi

if [ ! -f "$ENTRYPOINT" ] || [ ! -f "$MARKER_PY" ]; then
    _log "entrypoint or marker helper missing — nothing to do"
    exit 0
fi

# Re-pend any orphaned in-flight markers from a previous run that died mid-index
# (OOM / host stop / unit timeout). Safe here: we hold the runner flock, so no
# other tick is mid-claim — any *.inflight is necessarily from a dead run.
_marker reconcile-inflight >> "$LOG_FILE" 2>&1 || true

# Snapshot the pending markers up front (TSV: hash repo tools mode attempts age).
mapfile -t _MARKERS < <(_marker list 2>/dev/null)
if [ "${#_MARKERS[@]}" -eq 0 ]; then
    exit 0  # nothing queued — quiet no-op (the common case)
fi

for line in "${_MARKERS[@]}"; do
    IFS=$'\t' read -r hash repo tools mode _attempts age <<< "$line"
    if [ -z "${hash:-}" ] || [ -z "${repo:-}" ]; then
        continue
    fi

    if ! _idle_ok "${age:-0}"; then
        continue
    fi

    # Escalate a fast marker to full when the graph is due (and not backed off).
    # "full" is a cbm-only concept — gitnexus analyze ignores mode (always
    # incremental) — so a gitnexus-only marker must NOT escalate or it would
    # stamp .last-full and falsely suppress cbm's genuinely-needed full pass.
    run_mode="$mode"
    if [ "$mode" != "full" ] && { [ "$tools" = "cbm" ] || [ "$tools" = "both" ]; } \
        && _marker should-escalate --hash "$hash"; then
        run_mode="full"
        _log "escalating $repo to full (weekly/first full cbm index due)"
    fi

    # Move-aside claim so a commit landing mid-index is never dropped.
    if ! _marker claim --hash "$hash" >/dev/null 2>&1; then
        _log "could not claim marker $hash (already consumed?) — skipping"
        continue
    fi

    _log "indexing $repo (tools=$tools mode=$run_mode)"
    CODE_INTEL_INDEX_LOCK_SKIP_RC=75 \
        bash "$ENTRYPOINT" "$repo" "$tools" "$run_mode" >> "$LOG_FILE" 2>&1
    rc=$?

    case "$rc" in
        0)
            _marker consume --hash "$hash"
            # Only a successful FULL run that INCLUDED cbm records .last-full
            # (the escalation guard already ensures run_mode=full ⟹ cbm, but be
            # explicit — .last-full is shared across tools and gates cbm's full).
            if [ "$run_mode" = "full" ] && { [ "$tools" = "cbm" ] || [ "$tools" = "both" ]; }; then
                _marker stamp-full --hash "$hash"
            fi
            _log "indexed OK: $repo (mode=$run_mode)"
            ;;
        75)
            _marker restore --hash "$hash" >/dev/null
            _log "lock held / host-frozen — kept marker for $repo"
            ;;
        3)
            # A requested tool is missing from PATH (a persistent misconfig, not a
            # transient). Keep the marker (the present tool still wants indexing)
            # with no attempts penalty — but if this was an escalated full, back
            # off full so it doesn't re-escalate a heavy cbm full EVERY idle tick;
            # it degrades to cheap fast retries until PATH is fixed.
            _marker restore --hash "$hash" >/dev/null
            [ "$run_mode" = "full" ] && _marker mark-full-backoff --hash "$hash"
            _log "requested tool missing (rc=3) — kept marker, no penalty: $repo"
            ;;
        *)
            if [ "$run_mode" = "full" ] && [ "$mode" != "full" ]; then
                # Escalated-full failure: keep incremental fast indexing alive and
                # back off full so a doomed full (cbm can't resume) can't thrash.
                _marker restore --hash "$hash" >/dev/null
                _marker mark-full-backoff --hash "$hash"
                _log "escalated full failed (rc=$rc) — fell back to fast, backed off full: $repo"
            else
                state="$(_marker restore --hash "$hash" --attempts-inc)"
                _log "index failed (rc=$rc) — marker $state: $repo"
            fi
            ;;
    esac
done

exit 0
