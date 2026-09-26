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
#   after 24h so work is never starved forever) -> transactional claim -> run the
#   entrypoint FOREGROUND with CODE_INTEL_INDEX_LOCK_SKIP_RC=75 -> act on rc:
#     0  -> consume the in-flight row; stamp the full-success clock if applicable
#     75 -> lock held / host-frozen: restore the marker untouched (freeze-safe)
#     3  -> a requested tool was missing: restore, NO attempts penalty, loud log
#     *  -> failure: if this was an ESCALATED full (marker mode was fast), fall
#           back to fast + back off full (no attempts penalty); otherwise
#           restore with attempts+1 (moved to terminal failed state at the cap).
# The terminal action is persisted with the claim id before queue mutation. A
# later tick replays that exact action if the runner loses queue access or dies
# after the entrypoint returns, preventing a successful index from being run
# again or charged as a failure.
#
# Escalation: a fast marker whose graph has no recent full index runs as full
# (weekly refresh / first build), unless a recent full FAILED (backoff). cbm 0.9
# cannot resume a killed full, so the INITIAL from-scratch full rebuild is done
# SUPERVISED by an operator, not left to this loop (see the incident handoff).

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

SCRIPT_DIR="$(unset CDPATH; cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
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

_finish_outcome() {
    local hash="$1" action="$2" claim_id="$3" state
    if ! _marker remember-outcome --hash "$hash" --action "$action" \
        --claim-id "$claim_id" >> "$LOG_FILE" 2>&1; then
        _log "could not persist terminal action $action for $hash — stopping tick"
        return 76
    fi
    if ! state="$(_marker apply-outcome --hash "$hash" 2>> "$LOG_FILE")"; then
        _log "terminal action $action retained for $hash but could not be applied — stopping tick"
        return 76
    fi
    printf '%s\n' "$state"
}

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
# Never switch lock paths or proceed unlocked: orphan recovery and single-flight
# execution are only safe when every runner holds this SAME lock.
if ! mkdir -p "$LOCK_DIR" 2>/dev/null; then
    _log "cannot create runner lock directory — deferring"
    exit 75
fi
RUNNER_LOCK="$LOCK_DIR/code-intel-runner.lock"
if ! command -v flock >/dev/null 2>&1 || ! { exec 8>"$RUNNER_LOCK"; } 2>/dev/null; then
    _log "runner lock unavailable — deferring"
    exit 75
fi
lock_rc=0
flock -n -E 75 8 || lock_rc=$?
if [ "$lock_rc" -eq 75 ]; then
    _log "another runner tick is in progress — exiting"
    exit 0
elif [ "$lock_rc" -ne 0 ]; then
    _log "runner lock acquisition failed — deferring"
    exit 75
fi

if [ ! -f "$ENTRYPOINT" ] || [ ! -f "$MARKER_PY" ]; then
    _log "entrypoint or marker helper missing — nothing to do"
    exit 0
fi

# Re-pend any orphaned in-flight markers from a previous run that died mid-index
# (OOM / host stop / unit timeout). Safe here: we hold the runner flock, so no
# other tick is mid-claim — any inflight row is necessarily from a dead run.
if ! _marker reconcile-inflight >> "$LOG_FILE" 2>&1; then
    _log "inflight reconciliation failed — stopping tick without claiming new work"
    exit 76
fi

# Snapshot the pending markers up front (TSV: hash repo tools mode attempts age).
mapfile -t _MARKERS < <(_marker list 2>/dev/null)
if [ "${#_MARKERS[@]}" -eq 0 ]; then
    exit 0  # nothing queued — quiet no-op (the common case)
fi

for line in "${_MARKERS[@]}"; do
    # From the list snapshot we use ONLY hash + age (for the idle gate). repo/
    # tools/mode are read from the CLAIM below, not here — a concurrent commit
    # can coalesce the marker during the idle-sampling window, so the snapshot's
    # tools/mode may be stale (e.g. a gitnexus-only marker that became "both").
    IFS=$'\t' read -r hash _l_repo _l_tools _l_mode _l_attempts age <<< "$line"
    if [ -z "${hash:-}" ]; then
        continue
    fi

    if ! _idle_ok "${age:-0}"; then
        continue
    fi

    # Claim into a separate row FIRST (so a commit landing mid-index is never dropped),
    # then act on the CLAIMED state — the authoritative snapshot for this run.
    claimed="$(_marker claim --hash "$hash" 2>/dev/null)"
    if [ -z "$claimed" ]; then
        _log "could not claim marker $hash (already consumed?) — skipping"
        continue
    fi
    IFS=$'\t' read -r repo tools mode _attempts claim_id <<< "$claimed"
    if [ -z "${repo:-}" ] || [ -z "${claim_id:-}" ]; then
        _log "claimed marker $hash has incomplete ownership data — stopping tick"
        # Without the claim nonce a terminal event cannot be safely bound to
        # this generation. Reconciliation on the next tick will recover it.
        exit 76
    fi

    # Escalate a fast marker to full when the graph is due (and not backed off),
    # using the CLAIMED tools/mode. "full" is a cbm-only concept — gitnexus
    # analyze ignores mode (always incremental) — so a gitnexus-only marker must
    # NOT escalate or it would stamp the shared full-success clock and falsely suppress
    # cbm's genuinely-needed full pass.
    run_mode="$mode"
    if [ "$mode" != "full" ] && { [ "$tools" = "cbm" ] || [ "$tools" = "both" ]; }; then
        escalation_rc=0
        _marker should-escalate --hash "$hash" || escalation_rc=$?
        case "$escalation_rc" in
            0)
                run_mode="full"
                _log "escalating $repo to full (weekly/first full cbm index due)"
                ;;
            1)
                ;;
            *)
                _log "full-escalation query failed (rc=$escalation_rc) — restoring $repo and stopping tick"
                _finish_outcome "$hash" restore "$claim_id" >/dev/null || true
                exit 76
                ;;
        esac
    fi

    _log "indexing $repo (tools=$tools mode=$run_mode)"
    CODE_INTEL_INDEX_LOCK_SKIP_RC=75 \
        bash "$ENTRYPOINT" "$repo" "$tools" "$run_mode" >> "$LOG_FILE" 2>&1
    rc=$?

    case "$rc" in
        0)
            # Only a successful FULL run that INCLUDED cbm records the full-success clock
            # (the escalation guard already ensures run_mode=full ⟹ cbm, but be
            # explicit — the clock is shared across tools and gates cbm's full).
            if [ "$run_mode" = "full" ] && { [ "$tools" = "cbm" ] || [ "$tools" = "both" ]; }; then
                action="consume_full"
            else
                action="consume"
            fi
            _finish_outcome "$hash" "$action" "$claim_id" >/dev/null || exit 76
            _log "indexed OK: $repo (mode=$run_mode)"
            ;;
        75)
            _finish_outcome "$hash" restore "$claim_id" >/dev/null || exit 76
            _log "lock held / host-frozen — kept marker for $repo"
            ;;
        3)
            # Nothing indexed: a requested tool is missing or refused (a
            # persistent condition, not a transient). Keep the marker with no
            # attempts penalty — but if this was an escalated full, back off
            # full so it doesn't re-escalate a heavy cbm full EVERY idle tick;
            # it degrades to cheap fast retries until the condition is fixed.
            if [ "$run_mode" = "full" ]; then
                action="restore_backoff"
            else
                action="restore"
            fi
            _finish_outcome "$hash" "$action" "$claim_id" >/dev/null || exit 76
            _log "requested tool missing or refused (rc=3) — kept marker, no penalty: $repo"
            ;;
        4)
            # cbm leg completed; the gitnexus leg did not (missing, wrong
            # version, refused by admission control, or failed). Restoring the
            # whole marker would rebuild cbm every idle tick forever, so consume
            # cbm's durable work — and requeue a gitnexus-only marker FIRST, so
            # the unfinished leg survives: its blocking condition (sentinel
            # removed, install repaired, admission headroom) can clear without
            # a new trigger ever writing a marker. Written before the consume
            # so a runner death mid-branch restores the inflight "both" marker
            # and coalesces — the pending work is never lost. A refused leg then
            # retries like any rc-3 marker (no penalty); a failed leg retries
            # with attempts counting, bounded by MAX_ATTEMPTS.
            if ! _marker write --repo "$repo" --tools gitnexus --mode "$mode" >> "$LOG_FILE" 2>&1; then
                _log "could not requeue gitnexus leg for $repo — restoring combined marker"
                _finish_outcome "$hash" restore "$claim_id" >/dev/null || exit 76
            elif [ "$run_mode" = "full" ]; then
                _finish_outcome "$hash" consume_full "$claim_id" >/dev/null || exit 76
                _log "cbm indexed; gitnexus leg did not complete (rc=4) — consumed cbm (full stamp), requeued gitnexus: $repo"
            else
                _finish_outcome "$hash" consume "$claim_id" >/dev/null || exit 76
                _log "cbm indexed; gitnexus leg did not complete (rc=4) — consumed cbm, requeued gitnexus: $repo"
            fi
            ;;
        5)
            # gitnexus leg completed; the cbm leg did not (missing, kill-switch-
            # skipped, or failed). Same split as rc 4: requeue a cbm-only marker
            # before consuming so the skipped leg's request is not discarded
            # when the sentinel is later removed. Consume, but NEVER
            # consume_full: cbm never ran, so stamping the shared full-success
            # clock would suppress its genuinely-needed full pass for the
            # whole interval. The requeued fast marker re-escalates on its own
            # next tick if a full is still due.
            if ! _marker write --repo "$repo" --tools cbm --mode "$mode" >> "$LOG_FILE" 2>&1; then
                _log "could not requeue cbm leg for $repo — restoring combined marker"
                _finish_outcome "$hash" restore "$claim_id" >/dev/null || exit 76
            else
                _finish_outcome "$hash" consume "$claim_id" >/dev/null || exit 76
                _log "gitnexus indexed; cbm leg did not complete (rc=5) — consumed gitnexus, requeued cbm, no full stamp: $repo"
            fi
            ;;
        *)
            if [ "$run_mode" = "full" ] && [ "$mode" != "full" ]; then
                # Escalated-full failure: keep incremental fast indexing alive and
                # back off full so a doomed full (cbm can't resume) can't thrash.
                _finish_outcome "$hash" restore_backoff "$claim_id" >/dev/null || exit 76
                _log "escalated full failed (rc=$rc) — fell back to fast, backed off full: $repo"
            else
                state="$(_finish_outcome "$hash" restore_failure "$claim_id")" || exit 76
                _log "index failed (rc=$rc) — marker $state: $repo"
            fi
            ;;
    esac
done

exit 0
