#!/usr/bin/env bash
# Shared physical scope and pressure-watchdog functions.
# Sourced at the entrypoint's original definition point after its locks;
# globals, standalone routing, fallback and signal authority are unchanged.

# The scope's command gets its arguments as written. Systemd older than 254 refuses
# this option (exit 1, before it reaches --version), and only that drops it: anything
# else keeps it, and the probe below then decides. genesis.util.systemd does the same.
_SD_LITERAL=(--expand-environment=no)
_sd_literal_rc=0
/usr/bin/systemd-run "${_SD_LITERAL[@]}" --version >/dev/null 2>&1 || _sd_literal_rc=$?
if [ "$_sd_literal_rc" -eq 1 ]; then
    _SD_LITERAL=()
fi

_probe_scope() {
    # GitNexus batches run in app-capped.slice (a child of app.slice: same
    # ancestors, plus a kill counter that outlives the scope, which the disk
    # guardian reads to tell an own-cap kill from any other). CBM keeps
    # --slice-inherit: its admission checks headroom at the caller's ancestors.
    local -a slice_args=(--slice=app-capped.slice)
    [ "$2" = "cbm" ] && slice_args=(--slice-inherit)
    [ "$WORKLOAD_SLICE" = "1" ] && slice_args=(--slice=genesis-workload.slice)
    /usr/bin/systemd-run --user --scope "${slice_args[@]}" "${_SD_LITERAL[@]}" --quiet \
        -p "MemoryMax=$1" -p "MemorySwapMax=0" \
        -p "IOWeight=${IO_WEIGHT}" -p "CPUQuota=${CPU_QUOTA}" \
        -- /bin/true 2>/dev/null
}

_run_capped() {
    if [ "$_SCOPE_OK" = "1" ]; then
        local -a slice_args=(--slice=app-capped.slice)  # see _probe_scope
        [ "${_CI_SCOPE_INHERIT:-0}" = "1" ] && slice_args=(--slice-inherit)
        [ "$WORKLOAD_SLICE" = "1" ] && slice_args=(--slice=genesis-workload.slice)
        # _CI_SCOPE_UNIT (set by _run_with_watchdog) gives the scope a
        # deterministic name so the watchdog can freeze/thaw/stop it by unit.
        /usr/bin/systemd-run --user --scope "${slice_args[@]}" "${_SD_LITERAL[@]}" --quiet \
            ${_CI_SCOPE_UNIT:+--unit="$_CI_SCOPE_UNIT"} \
            -p "MemoryMax=${MEM_MAX}" -p "MemorySwapMax=0" \
            -p "IOWeight=${IO_WEIGHT}" -p "CPUQuota=${CPU_QUOTA}" \
            --description "code-intel index: $REPO_PATH" \
            -- /usr/bin/env \
                "CODE_INTEL_CHILD_REQUIRE_WORKLOAD_SLICE=$WORKLOAD_SLICE" \
                "CODE_INTEL_CHILD_CAP_BYTES=${CODE_INTEL_CHILD_ADMIT_CAP_BYTES:-}" \
                "CODE_INTEL_CHILD_RESERVE_BYTES=$CODE_INTEL_SIBLING_RESERVE_BYTES" \
                "CODE_INTEL_CHILD_SCOPE_UNIT=${_CI_SCOPE_UNIT:-}" \
                "CODE_INTEL_CHILD_REFUSAL_MARKER=${CODE_INTEL_CHILD_REFUSAL_MARKER:-}" \
                /bin/bash "$_CODE_INTEL_ENTRYPOINT" "${_CI_EXEC_MODE:---exec-indexer-with-oom-adj}" "$@"
    else
        # Fallback: polite scheduling + soft address-space cap. Mirrors the
        # run-codebase-memory launcher's degradation (never block on missing
        # systemd — CI and minimal containers must still work).
        local mem_kb="" mem_b=""
        mem_b="$(_genesis_mem_bytes "$MEM_MAX" 2>/dev/null || true)"
        if [ -n "$mem_b" ]; then
            mem_kb=$(( (mem_b + 1023) / 1024 ))
        else
            _log "WARNING: cannot parse '$MEM_MAX' for the rlimit fallback — running memory-uncapped (nice/ionice only)"
        fi
        (
            [ -n "$mem_kb" ] && ulimit -v "$mem_kb" 2>/dev/null
            if command -v ionice >/dev/null 2>&1; then
                exec nice -n 19 ionice -c 3 \
                    /bin/bash "$_CODE_INTEL_ENTRYPOINT" --exec-indexer-with-oom-adj "$@"
            else
                exec nice -n 19 \
                    /bin/bash "$_CODE_INTEL_ENTRYPOINT" --exec-indexer-with-oom-adj "$@"
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
_wd_alive() { kill -0 "$1" 2>/dev/null; }
_wd_wait() { sleep "$1"; }
_watchdog() {
    local kind="$1" target="$2" job_pid="$3"
    local wall start now bad=0 paused=0 pause_start=0 _WD_STOP_REASON=''
    wall="$(_wall_cap)"; start="$(date +%s)"
    local have_sampler=1
    command -v pressure_loadavg1 >/dev/null 2>&1 || have_sampler=0
    while _wd_alive "$job_pid"; do
        now="$(date +%s)"
        if [ "$(( now - start ))" -ge "$wall" ]; then
            _log "watchdog: wall cap ${wall}s reached (mode=$MODE) — killing index"
            _WD_STOP_REASON=wall
            _wd_kill "$kind" "$target" "$job_pid"; return 0
        fi
        if [ "$paused" = 1 ] && [ "$(( now - pause_start ))" -ge "$_WD_CONT_PAUSE_MAX" ]; then
            _log "watchdog: paused ${_WD_CONT_PAUSE_MAX}s continuously (system never calmed) — killing index"
            _WD_STOP_REASON=pressure_stop
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
        _wd_wait "$interval"
    done
    return 0
}

# _run_with_watchdog <tool_label> <command...>: run one tool under the watchdog.
_run_with_watchdog() {
    local label="$1"; shift
    local _SCOPE_OK="$_GN_SCOPE_OK"
    local scope_inherit=0
    local _CI_EXEC_MODE=--exec-indexer-with-oom-adj
    [ "$label" = "cbm" ] && _SCOPE_OK="$_CBM_SCOPE_OK"
    [ "$label" = "cbm" ] && scope_inherit=1
    [ "$label" != "cbm" ] || _CI_EXEC_MODE=--exec-managed-supervisor
    if [ "$WORKLOAD_SLICE" = "1" ] && [ "$_SCOPE_OK" != "1" ]; then
        if [ -n "${CODE_INTEL_CHILD_REFUSAL_MARKER:-}" ]; then
            printf '%s\n' refused > "$CODE_INTEL_CHILD_REFUSAL_MARKER" 2>/dev/null || true
        fi
        _log "refusing $label: requested workload routing is unavailable"
        return 125
    fi
    if [ "$_SCOPE_OK" = "1" ]; then
        local unit; unit="code-intel-$(printf '%s' "$REPO_PATH" | sha1sum | cut -c1-12)-${label}-$$"
        _CI_SCOPE_UNIT="$unit" _CI_SCOPE_INHERIT="$scope_inherit" _run_capped "$@" &
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
