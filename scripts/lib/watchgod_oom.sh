#!/usr/bin/env bash
# OOM event capture for scripts/tmp_watchgod.sh — SOURCED by it, never run.
# Uses the daemon's log() and queue_alert_try(), and its HOME-derived paths.

# ── OOM event capture (best-effort, cgroup v2) ───────────────
# A cgroup OOM kill silently collapses a CC session (tmux `exec claude` → claude
# is reaped → the last pane dies → the session ends) and leaves no durable trace:
# the kernel dmesg ring cycles and the kernel journal is usually unreadable from
# inside the container. This samples the container cgroup's CUMULATIVE oom_kill
# counter each poll and, on a NEW kill since the daemon started, records a
# timestamped snapshot (memory + top-RSS processes) and pages once — unless
# every new kill is a capped workload hitting its OWN cap. Read-only: it never
# kills or reclaims anything. Degrades to a no-op when the cgroup-v2 interface
# file is absent/unreadable (older layouts / non-cgroup2 hosts).
#
# ATTRIBUTION IS COUNTED, NEVER INFERRED FROM TEXT. Capped workloads run in the
# slices listed in OOM_CONTAINED_SLICES (app-capped.slice by default: the
# hostmetrics job runner, the cbm MCP wrapper and GitNexus index batches).
# cgroup v2 `memory.events` is hierarchical, and a slice outlives the scopes
# inside it, so each slice's own counters say exactly how many kills happened
# inside it and how many times a limit inside it fired. Every tick reads:
#   root   oom_kill / oom   (the container: every kill, every limit hit)
#   slice  oom_kill / oom   (per contained slice, with its directory inode)
# and pages when a new kill is NOT fully explained by a contained slice:
#   R1  root kills  > contained kills   — a victim outside the slices
#   R2  root ooms   > contained ooms    — a limit OUTSIDE them fired (the
#       container's own, or any ancestor's), whatever the victim; the kernel
#       usually picks a contained batch then (they volunteer, oom_score_adj)
#   R3  a slice with kills but no oom of its own — victim inside, trigger
#       outside (a limit above the container)
# plus every case that cannot be measured (an unreadable slice or one with no
# known baseline, a snapshot that would not hold still, a slice's counters going
# backwards, more contained kills than root kills): those PAGE. Accepted, stated:
# a tick that holds both a host-level kill and an own-cap kill in the SAME slice
# is inseparable at this layer; and the kernel raises `oom` microseconds before
# `oom_kill`, so a snapshot landing between them can page an own-cap kill early
# (R3) — the safe direction, never a dropped page. Containment is configured,
# not optional: an empty OOM_CONTAINED_SLICES in the environment keeps the
# default; set it in watchgod.conf to change it.
#
# Why not the journal (#3036 review): systemd v255 logs ONE notice per counter
# INCREASE however many processes died, newer systemd words it differently, and
# the user manager never sees system-manager units. The journal is used only to
# NAME units in the page, never to count.

_OOM_LEGACY_CURSOR_FILE="$(dirname "$LOG_FILE")/.oom_journal_cursor"
_OOM_UNIT_MESSAGE_ID="fe6faa94e7774663a0da52717891d8ef"  # SD_MESSAGE_UNIT_OUT_OF_MEMORY

_oom_field() {
    # $1 = a memory.events file, $2 = key. Echo its value; rc!=0 if unreadable.
    [[ -r "$1" ]] || return 1
    awk -v k="$2" '$1 == k { print $2; found = 1 } END { exit !found }' "$1" 2>/dev/null
}

_read_oom_kill() { _oom_field "$OOM_EVENTS_FILE" oom_kill; }

_oom_user_cgroup_dir() {
    # The user manager's cgroup directory, which the contained slices hang off.
    # Derived from this daemon's own membership (it runs under that manager);
    # OOM_USER_CGROUP_DIR overrides (tests). rc!=0 when it cannot be found —
    # then no slice resolves and every kill pages.
    if [[ -n "${OOM_USER_CGROUP_DIR:-}" ]]; then
        printf '%s' "$OOM_USER_CGROUP_DIR"
        return 0
    fi
    local _cg _mount _uid
    _mount=$(dirname "$OOM_EVENTS_FILE")
    _cg=$(awk -F: '$1 == "0" { print $3; exit }' /proc/self/cgroup 2>/dev/null) || _cg=""
    if [[ "$_cg" =~ ^(.*/user@[0-9]+\.service) ]]; then
        printf '%s%s' "$_mount" "${BASH_REMATCH[1]}"
        return 0
    fi
    # Not under the manager (a run from a login session scope): its standard
    # place for this uid, if it exists.
    _uid=$(id -u 2>/dev/null) || return 1
    [[ -d "${_mount}/user.slice/user-${_uid}.slice/user@${_uid}.service" ]] || return 1
    printf '%s' "${_mount}/user.slice/user-${_uid}.slice/user@${_uid}.service"
}

_oom_slice_relpath() {
    # systemd nests slices by dash: a-b-c.slice -> a.slice/a-b.slice/a-b-c.slice
    local _n="${1%.slice}" _acc="" _out="" _part
    local -a _parts
    IFS='-' read -ra _parts <<< "$_n"
    for _part in "${_parts[@]}"; do
        _acc="${_acc:+${_acc}-}${_part}"
        _out="${_out:+${_out}/}${_acc}.slice"
    done
    printf '%s' "$_out"
}

_oom_contained_slices() {
    # Echo the usable OOM_CONTAINED_SLICES names, one per line. Dropped (and so
    # NOT contained — fail toward paging): malformed names, the root and
    # app.slice themselves (they hold everything, genesis-server included), and
    # any name nested under another listed one (it would be counted twice).
    local _s _t _ok
    local -a _valid=() _list=()
    read -ra _list <<< "${OOM_CONTAINED_SLICES:-}"  # split, never globbed
    for _s in "${_list[@]}"; do
        [[ "$_s" =~ ^[A-Za-z0-9_]+(-[A-Za-z0-9_]+)*\.slice$ ]] || continue
        [[ "$_s" == "app.slice" ]] && continue
        _valid+=("$_s")
    done
    for _s in "${_valid[@]}"; do
        _ok=1
        for _t in "${_valid[@]}"; do
            [[ "$_s" == "$_t" ]] && continue
            [[ "${_s%.slice}" == "${_t%.slice}-"* ]] && _ok=0
        done
        (( _ok )) && printf '%s\n' "$_s"
    done | sort -u
}

_oom_slice_state() {
    # $1 = slice directory. Echo "inode,oom_kill,oom" | "-" (absent) | "?" (present
    # but unreadable — its count is unknown, so it can explain nothing).
    local _d="$1" _ino _k _o
    [[ -e "$_d" ]] || { printf '%s' "-"; return 0; }
    _ino=$(stat -c %i "$_d" 2>/dev/null) || { printf '%s' "?"; return 0; }
    _k=$(_oom_field "$_d/memory.events" oom_kill) || { printf '%s' "?"; return 0; }
    _o=$(_oom_field "$_d/memory.events" oom) || { printf '%s' "?"; return 0; }
    [[ "$_ino" =~ ^[0-9]+$ && "$_k" =~ ^[0-9]+$ && "$_o" =~ ^[0-9]+$ ]] \
        || { printf '%s' "?"; return 0; }
    printf '%s,%s,%s' "$_ino" "$_k" "$_o"
}

_oom_snapshot() {
    # Echo "root_kill root_oom name=state;name=state..." read as ONE consistent
    # moment: root, then slices, then root again; retried while the root moved
    # (a kill landing mid-read would otherwise credit a slice for a kill the root
    # has not counted yet, and the next tick would page it as uncontained).
    # rc 1 = root unreadable. rc 2 = never held still in 3 tries (caller pages).
    local _try _k1 _o1 _k2 _o2 _ucg _s _states
    _ucg=$(_oom_user_cgroup_dir) || _ucg=""
    for _try in 1 2 3; do
        _k1=$(_read_oom_kill) || return 1
        _o1=$(_oom_field "$OOM_EVENTS_FILE" oom) || _o1="?"
        _states=""
        while IFS= read -r _s; do
            [[ -n "$_s" ]] || continue
            if [[ -n "$_ucg" ]]; then
                _states+="${_s}=$(_oom_slice_state "${_ucg}/$(_oom_slice_relpath "$_s")");"
            else
                _states+="${_s}=?;"
            fi
        done < <(_oom_contained_slices)
        _k2=$(_read_oom_kill) || return 1
        _o2=$(_oom_field "$OOM_EVENTS_FILE" oom) || _o2="?"
        if [[ "$_k1" == "$_k2" && "$_o1" == "$_o2" ]]; then
            printf '%s %s %s' "$_k2" "$_o2" "${_states%;}"
            return 0
        fi
    done
    printf '%s %s %s' "$_k2" "$_o2" "${_states%;}"
    return 2
}

_oom_named_units() {
    # Unit names systemd logged an OOM notice for in the last few polls, comma
    # separated — FOR THE PAGE ONLY, never counted. The structured USER_UNIT
    # field, by stable MESSAGE_ID: no message text is parsed.
    command -v journalctl >/dev/null 2>&1 || return 1
    local _win=$(( POLL_INTERVAL * 2 + 60 ))
    journalctl --user -q --no-pager --since "-${_win} seconds" \
        MESSAGE_ID="$_OOM_UNIT_MESSAGE_ID" --output-fields=USER_UNIT -o cat 2>/dev/null \
        | awk 'NF && $0 !~ /\.slice$/' | sort -u | paste -sd, -
}

check_oom_events() {
    # $1 = the carried spec, "kill:oom:slicestates[:owed=<from>-<to>]" where
    # slicestates is "name=inode,kill,oom;name=-;name=?" (see _oom_slice_state).
    # Echoes the refreshed spec for the next tick and nothing else on stdout.
    #
    # `owed=` is present only while a page is owed (#2514): a decision to page
    # whose enqueue FAILED (a full disk is the likely cause). The decision is
    # never re-made — the counters have already moved — only the DELIVERY is
    # retried, at the start of every tick, before the counter is read. Success
    # clears it. Limit, stated: the spec lives in the daemon's memory, so a
    # watchgod restart while a page is owed drops it (the snapshot in OOM_LOG
    # and the WARN log line remain).
    local prev_spec="$1" base_spec="$1" owed_from="" owed_to="" owed_retry_failed=0
    if [[ "$prev_spec" =~ ^(.*):owed=([0-9]+)-([0-9]+)$ ]]; then
        base_spec="${BASH_REMATCH[1]}"
        owed_from="${BASH_REMATCH[2]}"
        owed_to="${BASH_REMATCH[3]}"
    fi
    if [[ -n "$owed_to" ]]; then
        if queue_alert_try emergency "watchgod:oom" "cgroup OOM kill(s) detected (delayed page)" \
            "OOM kill(s) in the container cgroup (oom_kill ${owed_from}->${owed_to}). This page is late: the first enqueue failed; which units were killed is in the watchgod log at that time. A CC session vanishing with no crash message is often this. Snapshot: ${OOM_LOG}" \
            "watchgod:oom:${owed_to}"; then
            log INFO "delayed OOM page queued (oom_kill ${owed_from}->${owed_to})"
            owed_from=""
            owed_to=""
        else
            owed_retry_failed=1
        fi
    fi
    local owed_suffix=""
    [[ -n "$owed_to" ]] && owed_suffix=":owed=${owed_from}-${owed_to}"

    local prev prev_oom prev_states
    prev="${base_spec%%:*}"
    local _rest=""
    [[ "$base_spec" == *:* ]] && _rest="${base_spec#*:}"
    prev_oom="${_rest%%:*}"
    prev_states=""
    [[ "$_rest" == *:* ]] && prev_states="${_rest#*:}"
    [[ "$prev" =~ ^[0-9]+$ ]] || prev=""
    [[ "$prev_oom" =~ ^[0-9]+$ ]] || prev_oom=""

    local _snap _snap_rc=0 cur cur_oom cur_states
    _snap=$(_oom_snapshot) || _snap_rc=$?
    (( _snap_rc == 1 )) && { printf '%s' "${base_spec}${owed_suffix}"; return 0; }
    read -r cur cur_oom cur_states <<< "$_snap"
    [[ "$cur" =~ ^[0-9]+$ ]] || { printf '%s' "${base_spec}${owed_suffix}"; return 0; }
    [[ "$cur_oom" =~ ^[0-9]+$ ]] || cur_oom=""

    # LATE ARM: no usable baseline (memory.events unreadable at startup, or a
    # malformed spec). Baseline here; this tick decides nothing.
    if [[ -z "$prev" ]]; then
        log INFO "OOM event capture armed late (baseline oom_kill=${cur})"
        printf '%s' "${cur}:${cur_oom}:${cur_states}${owed_suffix}"
        return 0
    fi

    if (( cur > prev )); then
        local n=$(( cur - prev )) stamp
        stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        {
            echo "# OOM event ${stamp}: cgroup oom_kill ${prev} -> ${cur} (+${n})"
            echo "## memory (MB):"; free -m 2>/dev/null | head -2
            echo "## top RSS:"; ps -eo pid,rss,comm --sort=-rss 2>/dev/null | head -12
            echo
        } >> "$OOM_LOG" 2>/dev/null || true
        log WARN "cgroup OOM kill detected (oom_kill ${prev} -> ${cur}); snapshot → ${OOM_LOG}"

        # Per-slice deltas against the PREVIOUS tick's states.
        local -A _prevst=()
        local _e _name _cs _ps _ino _k _o _pino _pk _po _dk _do
        local -a _pe=() _ce=()
        local _sum_k=0 _sum_o=0 _why="" _where=""
        IFS=';' read -ra _pe <<< "$prev_states"
        for _e in "${_pe[@]}"; do [[ "$_e" == *=* ]] && _prevst["${_e%%=*}"]="${_e#*=}"; done
        IFS=';' read -ra _ce <<< "$cur_states"
        for _e in "${_ce[@]}"; do
            [[ "$_e" == *=* ]] || continue
            _name="${_e%%=*}"; _cs="${_e#*=}"; _ps="${_prevst[$_name]-}"
            [[ "$_cs" == "-" ]] && continue                 # absent: explains nothing
            if [[ "$_cs" == "?" ]]; then
                _why="${_why:-${_name} unreadable}"; continue
            fi
            IFS=',' read -r _ino _k _o <<< "$_cs"
            if [[ "$_ps" == "-" ]]; then
                _dk=$_k; _do=$_o                             # born since last tick
            elif [[ "$_ps" =~ ^[0-9]+,[0-9]+,[0-9]+$ ]]; then
                IFS=',' read -r _pino _pk _po <<< "$_ps"
                if [[ "$_pino" != "$_ino" ]]; then
                    _dk=$_k; _do=$_o                         # recreated since last tick
                else
                    _dk=$(( _k - _pk )); _do=$(( _o - _po ))
                fi
            else
                _why="${_why:-${_name} had no known baseline}"; continue
            fi
            if (( _dk < 0 || _do < 0 )); then
                _why="${_why:-${_name} counters went backwards}"; continue
            fi
            if (( _dk > 0 )); then
                _where="${_where:+${_where}, }${_name} +${_dk}"
                (( _do == 0 )) && _why="${_why:-${_name} lost ${_dk} process(es) with no limit of its own firing (a limit above it did)}"
            fi
            _sum_k=$(( _sum_k + _dk )); _sum_o=$(( _sum_o + _do ))
        done

        local _d_root_oom=""
        [[ -n "$cur_oom" && -n "$prev_oom" ]] && _d_root_oom=$(( cur_oom - prev_oom ))
        if (( _snap_rc == 2 )); then
            _why="counters would not hold still to be read"
        elif [[ -z "$_why" ]]; then
            if (( _sum_k < n )); then
                _why="$(( n - _sum_k )) of ${n} kill(s) outside the contained slices"
            elif (( _sum_k > n )); then
                _why="contained slices report more kills (${_sum_k}) than the container (${n})"
            elif [[ -z "$_d_root_oom" ]]; then
                _why="the container's limit counter is unreadable, so the trigger is unknown"
            elif (( _d_root_oom > _sum_o )); then
                _why="a memory limit outside the contained slices fired"
            fi
        fi
        local _names
        _names=$(_oom_named_units) || _names=""
        echo "## kills: ${n} (contained ${_sum_k}${_where:+ in ${_where}}); limit hits: container ${_d_root_oom:-?}, contained ${_sum_o}; journal names: ${_names:-none}${_why:+; PAGE: ${_why}}" \
            >> "$OOM_LOG" 2>/dev/null || true

        if [[ -z "$_why" ]]; then
            log WARN "OOM kill(s) contained in [${_where}] — their own cap fired; not paging (snapshot kept${_names:+; recent notices: ${_names}})"
        else
            # Emergency tier (pages): an OOM kill is a discrete serious event —
            # the usual reason a CC session vanishing with no crash message —
            # not routine tier pressure (2026-08-19 decision). Deduped per
            # distinct oom_kill total.
            local _from="$prev" _earlier=""
            if (( owed_retry_failed == 1 )) && [[ -n "$owed_to" ]]; then
                _from="$owed_from"
                _earlier=" Includes earlier kill(s) oom_kill ${owed_from}->${owed_to} whose page could not be queued then."
            fi
            local _detail="${_why}; recent journal OOM notices: ${_names:-none found}"
            if queue_alert_try emergency "watchgod:oom" "cgroup OOM kill(s) detected" \
                "${n} process(es) OOM-killed in the container cgroup (oom_kill ${prev}->${cur}; ${_detail}).${_earlier} A CC session vanishing with no crash message is often this. Snapshot: ${OOM_LOG}" \
                "watchgod:oom:${cur}"; then
                owed_from=""
                owed_to=""
            else
                # Decided, not delivered: owe it; every later tick retries
                # delivery. Logged once per decided page, WITH the attribution
                # the delayed page cannot carry (Devin review of #2706).
                log WARN "OOM page could not be queued (oom_kill ${prev}->${cur}; ${_detail}); retrying every poll"
                owed_from="$_from"
                owed_to="$cur"
            fi
        fi
        # Bound the OOM log (retention discipline); keep the latest ~1000 lines.
        local oom_lines
        oom_lines=$(wc -l < "$OOM_LOG" 2>/dev/null || echo 0)
        if (( ${oom_lines:-0} > 1000 )); then
            tail -n 1000 "$OOM_LOG" > "${OOM_LOG}.tmp" 2>/dev/null && mv "${OOM_LOG}.tmp" "$OOM_LOG" 2>/dev/null || true
        fi
    fi
    owed_suffix=""
    [[ -n "$owed_to" ]] && owed_suffix=":owed=${owed_from}-${owed_to}"
    # A transient unreadable root oom counter must not disarm the trigger check:
    # carry the last known value (this tick already paged if it mattered).
    printf '%s' "${cur}:${cur_oom:-$prev_oom}:${cur_states}${owed_suffix}"
}

_oom_arm_baseline() {
    # Echo the initial spec ("" = monitoring unavailable). Also removes the
    # journal cursor file earlier versions kept: nothing reads it now.
    rm -f "$_OOM_LEGACY_CURSOR_FILE" 2>/dev/null || true
    local _snap _rc=0 k o s
    _snap=$(_oom_snapshot) || _rc=$?
    (( _rc == 1 )) && { printf '%s' ""; return 0; }
    read -r k o s <<< "$_snap"
    [[ "$k" =~ ^[0-9]+$ ]] || { printf '%s' ""; return 0; }
    [[ "$o" =~ ^[0-9]+$ ]] || o=""
    printf '%s' "${k}:${o}:${s}"
}
