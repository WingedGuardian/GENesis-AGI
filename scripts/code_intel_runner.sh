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
# This intermediate binary still refuses automatic consumption at readiness.
# The checked consumer below is prepared for the later complete protocol: one
# provider per typed claim and complete context-bound native outcomes. Recovery
# selection and the final physical exclusion gate still precede positive readiness.

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

if declare -F _ci_source >/dev/null; then
    SCRIPT_DIR="${_CI_ORIGIN%/*}"
else
    SCRIPT_DIR="$(unset CDPATH; cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
fi
# Captured provenance check; native dispatch uses the original owner below.
ENTRYPOINT="${CODE_INTEL_ENTRYPOINT:-$SCRIPT_DIR/lib/code_intel_index.sh}"
MARKER_PY="$SCRIPT_DIR/lib/index_marker.py"
# shellcheck source=lib/proc_pressure.sh
if declare -F _ci_source >/dev/null; then _ci_source proc_pressure.sh || exit 76
else . "$SCRIPT_DIR/lib/proc_pressure.sh"; fi

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

_marker() {
    if declare -F _ci_queue >/dev/null; then _ci_queue "$@"
    else python3 "$MARKER_PY" "$@"; fi
}

# Checked machine transport. _WIRE is replaced only after the child has exited;
# the list snapshot is separately retained before claim/escalation reuse it.
_WIRE=()

_queue_capture() {
    : > "$QUEUE_OUTPUT" || return 76
    _marker "$@" > "$QUEUE_OUTPUT" 2>> "$LOG_FILE"
}

_read_wire() {
    _WIRE=()
    local field=""
    while IFS= read -r -d '' field; do
        _WIRE+=("$field")
        field=""
    done < "$QUEUE_OUTPUT" || return 1
    [[ -z "$field" ]]
}

_wire_hash() { [[ "$1" =~ ^[0123456789abcdef]{16}$ ]]; }
_wire_provider() { [[ "$1" == cbm || "$1" == gitnexus ]]; }
_wire_mode() { [[ "$1" == fast || "$1" == moderate || "$1" == full ]]; }

_wire_number() {
    local raw="$1" parsed=""
    [[ "$raw" =~ ^(0|[123456789][0123456789]*)$ && ${#raw} -le 19 ]] || return 1
    printf -v parsed '%d' "$raw" 2>/dev/null || return 1
    [[ "$parsed" == "$raw" ]]
}

_pending_wire() {
    local i="$1"
    [[ "${_WIRE[i]}" == 1 && "${_WIRE[i+1]}" == pending ]] || return 1
    _wire_hash "${_WIRE[i+2]}" && [[ -n "${_WIRE[i+3]}" ]] &&
        _wire_provider "${_WIRE[i+4]}" && _wire_number "${_WIRE[i+5]}"
}

# Check at each queue boundary; the sentinel can change while a tick runs.
# Unresolvable configuration defers CBM, as the entrypoint does.
_cbm_disabled() {
    if declare -F _ci_available >/dev/null; then
        ! _ci_available "$HOME/.genesis/config/codebase-managed.json" "$1" \
            "${CODE_INTEL_INDEX_PERSISTENCE:-true}" >/dev/null 2>> "$LOG_FILE"
        return $?
    fi
    ! /usr/bin/python3 -I "$SCRIPT_DIR/codebase_managed.py" \
        --config "$HOME/.genesis/config/codebase-managed.json" available --repo "$1" \
        --persistence "${CODE_INTEL_INDEX_PERSISTENCE:-true}" \
        >/dev/null 2>> "$LOG_FILE"
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

if declare -F _ci_entry >/dev/null; then
    if [ "$ENTRYPOINT" != "$_CI_EXPECTED_ENTRY" ]; then
        _log "captured runner requires its captured entrypoint — deferring"
        exit 75
    fi
elif [ ! -f "$ENTRYPOINT" ] || [ ! -f "$MARKER_PY" ]; then
    _log "entrypoint or marker helper missing — deferring without reading the queue"
    exit 75
fi

# Partial protocol upgrades may enqueue and expose status, but never reconcile,
# claim, or launch automatically. Only the later strict cutover can enable this.
# An incomplete protocol (the helper's dedicated status 10) is the expected
# steady state until then, so it exits 0 like a busy tick. Any other status,
# including 1 from a Python import crash or a failed log redirection, is a real
# failure and exits 76 so the unit shows failed. This protocol has no ready
# state yet, so status 0 is an anomaly too: it stops the tick rather than
# proceeding, until the strict cutover defines what ready means. The helper's
# per-tick JSON goes to /dev/null so the pause adds one log line per tick, not
# two; its errors still reach the log.
protocol_rc=0
_marker automatic-ready > /dev/null 2>> "$LOG_FILE" || protocol_rc=$?
case "$protocol_rc" in
    0) _log "readiness helper reported ready, but this protocol has no ready state — stopping tick"; exit 76 ;;
    10) _log "automatic queue protocol incomplete — deferring"; exit 0 ;;
    75) _log "queue busy at readiness — deferring"; exit 0 ;;
    *) _log "automatic queue protocol observation failed — stopping tick"; exit 76 ;;
esac
if ! declare -F _ci_owner >/dev/null; then
    # Older rendered units enter this shell directly. Before any queue action,
    # restart through the same captured source role as the current unit. The
    # raw waiter must release its lock so the captured child can acquire it;
    # another tick may win that race and the child then defers normally.
    _log "entering captured runner before typed queue dispatch"
    exec 8>&-
    capture_rc=0
    /usr/bin/python3 -I -S "$SCRIPT_DIR/lib/code_intel_source.py" shell runner runner || capture_rc=$?
    case "$capture_rc" in 0|75|76) exit "$capture_rc" ;;
        *) _log "captured runner startup failed — stopping tick"; exit 76 ;;
    esac
fi

# One owned, private, ephemeral transport path. It is never recovery evidence.
if ! QUEUE_OUTPUT="$(umask 077; mktemp -- "$GENESIS_HOME/.code-intel-output.XXXXXXXX.tmp")"; then
    _log "cannot create queue transport — stopping tick"
    exit 76
fi
trap 'rm -f -- "$QUEUE_OUTPUT"' EXIT

# Preserve legacy settlement, then reconcile typed claims from durable facts.
reconcile_rc=0
_marker reconcile-inflight >> "$LOG_FILE" 2>&1 || reconcile_rc=$?
case "$reconcile_rc" in 0) ;;
    75) _log "queue busy at inflight reconciliation — deferring"; exit 0 ;;
    *) _log "inflight reconciliation failed — stopping tick without claiming new work"; exit 76 ;;
esac
reconcile_rc=0
_marker reconcile-native >> "$LOG_FILE" 2>&1 || reconcile_rc=$?
case "$reconcile_rc" in 0) ;;
    75) _log "queue busy at native reconciliation — deferring"; exit 0 ;;
    *) _log "native reconciliation failed — stopping tick without claiming new work"; exit 76 ;;
esac

list_rc=0
_queue_capture list-legs --nul || list_rc=$?
if [ "$list_rc" -eq 75 ]; then
    _log "queue busy at pending-list — deferring"
    exit 0
fi
if [ "$list_rc" -ne 0 ] || ! _read_wire; then
    _log "pending-list observation failed (rc=$list_rc) — stopping tick"
    exit 76
fi
if (( ${#_WIRE[@]} % 6 != 0 )); then
    _log "invalid pending-list framing — stopping tick"
    exit 76
fi
# Validate EVERY row before the first claim; a valid prefix cannot authorize
# partial processing of a list whose later record is malformed.
for ((i=0; i<${#_WIRE[@]}; i+=6)); do
    if ! _pending_wire "$i"; then
        _log "invalid pending-list record — stopping tick"
        exit 76
    fi
done
_MARKERS=("${_WIRE[@]}")

for ((i=0; i<${#_MARKERS[@]}; i+=6)); do
    hash="${_MARKERS[i+2]}"
    listed_repo="${_MARKERS[i+3]}"
    provider="${_MARKERS[i+4]}"
    age="${_MARKERS[i+5]}"
    if [ "$provider" = cbm ] && _cbm_disabled "$listed_repo"; then
        _log "CBM disabled — leaving request pending: $listed_repo"
        continue
    fi
    _idle_ok "$age" || continue

    claim_rc=0
    _queue_capture claim-native --hash "$hash" --provider "$provider" --nul || claim_rc=$?
    case "$claim_rc" in 0|1) ;;
        75) _log "queue busy at claim — deferring"; exit 0 ;;
        *) _log "claim failed (rc=$claim_rc) — stopping tick"; exit 76 ;;
    esac
    if ! _read_wire; then
        _log "invalid claim framing — stopping tick"
        exit 76
    fi
    if [ "$claim_rc" -eq 1 ]; then
        if [ "${#_WIRE[@]}" -eq 4 ] &&
            [[ "${_WIRE[0]}" == 1 && "${_WIRE[1]}" == unclaimed &&
               "${_WIRE[2]}" == "$hash" && "${_WIRE[3]}" == "$provider" ]]; then
            continue
        fi
        _log "claim predicate lacks a matching acknowledgement — stopping tick"
        exit 76
    fi
    if [ "${#_WIRE[@]}" -ne 7 ]; then
        _log "invalid claim arity — stopping tick"
        exit 76
    fi
    if ! [[ "${_WIRE[0]}" == 1 && "${_WIRE[1]}" == claim &&
            "${_WIRE[2]}" == "$hash" && "${_WIRE[4]}" == "$provider" &&
            -n "${_WIRE[3]}" && -n "${_WIRE[6]}" ]] || ! _wire_mode "${_WIRE[5]}"; then
        _log "invalid claimed ownership snapshot — stopping tick"
        exit 76
    fi
    repo="${_WIRE[3]}"
    mode="${_WIRE[5]}"
    context="${_WIRE[6]}"
    if [ "$provider" = cbm ] && _cbm_disabled "$repo"; then
        reconcile_rc=0
        _marker reconcile-native >> "$LOG_FILE" 2>&1 || reconcile_rc=$?
        case "$reconcile_rc" in 0) ;;
            75) _log "queue busy reconciling disabled claim — deferring"; exit 0 ;;
            *) _log "disabled claim reconciliation failed — stopping tick"; exit 76 ;;
        esac
        _log "CBM disabled after claim — restored request: $repo"
        continue
    fi

    run_mode="$mode"
    if [ "$provider" = cbm ] && [ "$mode" != full ]; then
        escalation_rc=0
        _queue_capture should-escalate --hash "$hash" --nul || escalation_rc=$?
        escalation_ok=0
        if [ "$escalation_rc" -eq 0 ] && _read_wire && [ "${#_WIRE[@]}" -eq 4 ]; then
            if [[ "${_WIRE[0]}" == 1 && "${_WIRE[1]}" == escalation &&
                  "${_WIRE[2]}" == "$hash" && ( "${_WIRE[3]}" == 0 || "${_WIRE[3]}" == 1 ) ]]; then
                escalation_ok=1
                if [ "${_WIRE[3]}" = 1 ]; then run_mode=full; fi
            fi
        fi
        if [ "$escalation_rc" -eq 75 ]; then
            reconcile_rc=0
            _marker reconcile-native >> "$LOG_FILE" 2>&1 || reconcile_rc=$?
            case "$reconcile_rc" in 0|75) ;;
                *) _log "unstarted claim reconciliation failed — stopping tick"; exit 76 ;;
            esac
            _log "queue busy at full-escalation check — retained claim, deferring"
            exit 0
        fi
        if [ "$escalation_ok" -ne 1 ]; then
            _log "full-escalation observation failed — restoring claim and stopping tick"
            _marker reconcile-native >> "$LOG_FILE" 2>&1 || true
            exit 76
        fi
    fi

    _log "indexing $repo (tools=$provider mode=$run_mode)"
    : > "$QUEUE_OUTPUT" || exit 76
    if ! _ci_owner "$context" "$run_mode" "$HOME/.genesis/config/codebase-managed.json" \
            > "$QUEUE_OUTPUT" 2>> "$LOG_FILE"; then
        _log "owner result unavailable — preserving durable outcome and stopping tick"
        exit 76
    fi
    if ! _read_wire || [ "${#_WIRE[@]}" -ne 6 ]; then
        _log "invalid native-outcome framing — stopping tick"
        exit 76
    fi
    if ! [[ "${_WIRE[0]}" == 1 && "${_WIRE[1]}" == native-outcome &&
            "${_WIRE[2]}" == "$hash" && "${_WIRE[3]}" == "$provider" &&
            "${_WIRE[4]}" == "$context" &&
            ( "${_WIRE[5]}" == consumed || "${_WIRE[5]}" == pending || "${_WIRE[5]}" == failed ) ]]; then
        _log "native outcome differs from this claim — stopping tick"
        exit 76
    fi
    _log "native request ${_WIRE[5]}: $repo (tools=$provider mode=$run_mode)"
done

exit 0
