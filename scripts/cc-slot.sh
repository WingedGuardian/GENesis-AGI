#!/usr/bin/env bash
# cc-slot.sh — Persistent tmux slot for Claude Code sessions.
#
# THE one interactive launcher: every door (SSH slot hostnames, manual SSH,
# the dashboard web terminal via the bashrc claude() wrapper) converges here,
# on the same attach-or-create tmux sessions. Idempotent by construction — a
# door walked twice attaches the SAME claude instead of spawning a second one.
#
# Usage: cc-slot.sh <hostname>               SSH RemoteCommand; parses a
#                                            hostname like "genesis-3-4" to
#                                            slot 4 -> session "cc-4"
#        cc-slot.sh manual [claude-args...]  manual/dashboard door: prints the
#                                            slot map, takes the LOWEST free
#                                            slot, forwards extra args to
#                                            claude inside the session

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

# SSH RemoteCommand doesn't source .bashrc (interactive guard) — set PATH explicitly
export PATH="$HOME/.n/bin:$HOME/.bun/bin:$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"

# SSH from Windows sends no locale, so tmux marks the client non-UTF-8 and
# renders every non-ASCII glyph as "_". Force a UTF-8 locale for the client.
export LANG="${LANG:-C.UTF-8}"

GENESIS_ROOT="${HOME}/genesis"
SESSION_PREFIX="cc"

# --- Parse slot number from hostname (or allocate one in manual mode) ---
if [[ $# -lt 1 ]]; then
    echo "Usage: cc-slot.sh <hostname>            (e.g., genesis-3-4)" >&2
    echo "       cc-slot.sh manual [claude-args]  (lowest free slot)" >&2
    exit 1
fi

MODE_ARG="$1"
shift
# Verdict for a slot: ALIVE | POISONED | UNKNOWN (empty on any failure). ONE
# definition, and every consumer shares it so they can never drift into
# disagreeing: the manual-mode slot map, the heal decision, the RE-CHECK, and
# the `--idle` safe-to-type question that is the LAST gate before typing —
# the re-check is deliberately no longer the final one. Defined
# here — above the manual-mode map that is its first caller — because bash
# resolves a function only once its definition has RUN; GENESIS_ROOT is set
# just above. An unavailable probe prints nothing, and every caller treats
# that as "no verdict".
_probe_liveness() {
    local out=""
    [ -x "${GENESIS_ROOT}/.venv/bin/python" ] || { printf '%s' ""; return 0; }
    out=$(timeout 15 "${GENESIS_ROOT}/.venv/bin/python" \
        -m genesis.cc.slot_liveness "$@" 2>/dev/null | sed -n '1p' || true)
    printf '%s' "$out"
}

# The slot map's variant. Same verdict, MUCH shorter leash: the map is
# cosmetic and runs once per listed slot in the interactive login path, so a
# hung interpreter would stall the door by 15s PER SLOT (MEASURED: 45s across
# three slots, and this install carries seven). The heal path keeps the long
# budget because there the verdict decides whether to touch a live pane; here
# it only decides whether to print a note.
_MAP_PROBE_BUDGET=3
_MAP_PROBE_GAVE_UP=0
_map_verdict() {
    local out="" rc=0
    [ -x "${GENESIS_ROOT}/.venv/bin/python" ] || { printf '%s' ""; return 0; }
    # ONE timeout for the whole map, not one per slot. A hung interpreter hangs
    # identically for every slot, so paying the budget N times buys nothing and
    # costs N x budget on an interactive login (MEASURED healthy: 146ms a slot,
    # 8 slots on this install; MEASURED hung, before this: 5 slots x 3s).
    # The timeout is REPORTED to the caller rather than recorded here: this
    # function's output is read through `$(...)`, which is a SUBSHELL, so a
    # variable set in here never reaches the loop — the first attempt did
    # exactly that and measured no better than per-slot.
    [ "$_MAP_PROBE_GAVE_UP" = "1" ] && { printf '%s' ""; return 0; }
    out=$(timeout "$_MAP_PROBE_BUDGET" "${GENESIS_ROOT}/.venv/bin/python" \
        -m genesis.cc.slot_liveness "$@" 2>/dev/null | sed -n '1p') || rc=$?
    # 124 is timeout(1)'s "deadline expired".
    [ "$rc" = "124" ] && { printf '%s' "TIMEOUT"; return 0; }
    printf '%s' "$out"
}

# Extra claude args exist only in manual mode (SSH RemoteCommand passes %n only).
CLAUDE_EXTRA_ARGS=("$@")

if [[ "$MODE_ARG" == "manual" ]]; then
    # Manual/dashboard door. Show what already exists so reattach is the
    # visible easy path, then take the lowest slot with no live session.
    # New-by-default is deliberate: auto-reattach would trap "I want a fresh
    # session" in a loop; reattach stays one printed command away.
    slot_map=$(tmux list-sessions \
        -F '#{session_name}|#{session_attached}|#{t:session_activity}' \
        2>/dev/null | grep "^${SESSION_PREFIX}-" || true)
    if [[ -n "$slot_map" ]]; then
        echo "Existing slots (reattach: tmux attach -t <name>):" >&2
        while IFS='|' read -r name attached activity; do
            state="detached"
            [[ "$attached" -ge 1 ]] && state="attached"
            # Say when a slot is alive but running NO claude. Manual allocation
            # deliberately never takes over an existing session, and only the
            # hostname door heals its own slot — so without this the map lists a
            # stuck slot as though it were healthy, and the `tmux attach` hint
            # above sends the operator straight back to the bare prompt without
            # ever passing the door that would relaunch it. One probe per listed
            # slot; anything other than an explicit POISONED prints nothing, so
            # an unavailable probe never renders as a verdict.
            note=""
            # `|| true` is load-bearing, not defensive noise: the script runs
            # under `set -euo pipefail`, so a `var=$(cmd | filter)` whose FIRST
            # component fails takes the whole door down with no message —
            # pipefail promotes the failure past `tr`, and `set -e` exits. A
            # listed session going away before it is inspected is ordinary (the
            # tmux server shuts down the moment the last slot's claude exits),
            # and `tmux list-panes` on a missing session exits 1. On the manual
            # door — the dashboard terminal and manual SSH — that would drop the
            # operator at a bare prompt with no claude and no error. Every
            # sibling line of this shape in this file carries the same guard.
            _map_pids=$(tmux list-panes -s -t "=${name}" -F '#{pane_pid}' 2>/dev/null | tr '\n' ' ' || true)
            _map_v=""
            [ -n "$_map_pids" ] && _map_v=$(_map_verdict $_map_pids)
            # Set in the LOOP's shell, not inside the substitution above.
            [ "$_map_v" = "TIMEOUT" ] && _MAP_PROBE_GAVE_UP=1
            if [[ "$_map_v" == "POISONED" ]]; then
                note="  (no claude running — 'tmux attach' will NOT relaunch it; re-enter through this slot's door)"
            fi
            echo "  ${name}  ${state}  (last activity: ${activity})${note}" >&2
        done <<<"$slot_map"
    fi
    SLOT=1
    # '=' forces exact-name match: a bare -t is prefix-matched by tmux, so
    # cc-1 would falsely read as existing whenever only cc-10 does.
    while tmux has-session -t "=${SESSION_PREFIX}-${SLOT}" 2>/dev/null; do
        SLOT=$((SLOT + 1))
    done
else
    SLOT="${MODE_ARG##*-}"

    if ! [[ "$SLOT" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: Invalid slot '$SLOT' (parsed from '$MODE_ARG')." >&2
        echo "Slot must be a positive integer (1, 2, 3, ...)." >&2
        exit 1
    fi
fi

SESSION_NAME="${SESSION_PREFIX}-${SLOT}"

# Handle nested tmux
unset TMUX

# Load operator config levers (~/.genesis/cc-slot.env) BEFORE the capacity gate,
# so its GENESIS_CC_* tunables actually take effect there (an SSH RemoteCommand
# does NOT source .bashrc). This file is also home to the permission-mode lever
# (GENESIS_CC_PERMISSION_MODE) and the OAuth-durability lever
# (GENESIS_CC_SLOT_OAUTH), both consumed later. `|| echo` keeps a malformed file
# from aborting the login under `set -e`.
if [ -f "${HOME}/.genesis/cc-slot.env" ]; then
    . "${HOME}/.genesis/cc-slot.env" \
        || echo "cc-slot: warning: ~/.genesis/cc-slot.env sourced with errors (continuing)" >&2
fi
# A sourced var is a shell var, NOT exported — EXPORT the cap levers so the Python
# gate subprocess inherits them. Only export those actually set (an empty export
# would still read as unset → default, but keep the env clean).
for _lever in GENESIS_CC_SYSTEM_RESERVE_MB GENESIS_CC_PER_SESSION_MB \
              GENESIS_CC_OOM_FLOOR_MB GENESIS_CC_EMERGENCY_SLOTS; do
    [ -n "${!_lever:-}" ] && export "$_lever"
done
unset _lever

# --- Session cap (capacity model; decision delegated to genesis.cc.session_cap) ---
# The launcher only GATHERS inputs and EXECUTES the returned action; the pure,
# unit-tested Python gate decides (SAFE_CAP from MemTotal — stable, does NOT
# collapse as sessions run, unlike the old MemAvailable/900 formula that locked
# the operator out at "3/2"). Reattach to an existing slot bypasses this entirely.
# The gate never turns an operator away by the CAP: ALLOW → proceed; DENY →
# message+exit (dashboard/"normal method" — no SSH_CONNECTION — over cap); RECLAIM →
# interactive pick-a-session-to-end (any SSH login). RECLAIM can still decline in two
# honest corners (no controlling TTY, or an OOM-floor breach with nothing to trade) —
# both guide to reattach, never risk an OOM. It fails OPEN — a Python error falls back
# to a MemTotal-based STATIC cap (never the collapsing free-RAM formula), so a broken
# venv can never strand the operator. Config levers (~/.genesis/cc-slot.env):
# GENESIS_CC_SYSTEM_RESERVE_MB / _PER_SESSION_MB / _OOM_FLOOR_MB / _EMERGENCY_SLOTS.

# List live numeric cc-N slots to stderr (shared by DENY + fail-open paths).
_cap_list_slots() {
    echo "Active sessions (reattach: tmux attach -t <name>):" >&2
    tmux list-sessions \
        -F '  #{session_name}  (#{?session_attached,ATTACHED,detached}, idle since #{t:session_activity})' \
        2>/dev/null | grep -E "^  ${SESSION_PREFIX}-[0-9]+ " >&2 || true
}

# Interactive reclaim (operator origin, or the fail-open path with a TTY): offer
# to END a session to make room, or cancel and reattach. Returns 0 to proceed
# with the launch; exits on cancel / invalid / no-TTY.
_cap_reclaim() {
    local msg="$1" reason="${2:-}" choice victim victim_att _confirm row nm att act state meta i=1
    local -a names rows
    # Build the reclaim list from FULLY-anchored bare cc-N names (`grep -xE`, the
    # same full-line match the `existing` count uses), then query each session's
    # attach/activity BY EXACT NAME. Never split a single combined
    # `name|attached|activity` line: a session whose NAME embeds `|` (e.g. one an
    # attacker crafts as `cc-9|0|x`) would desync the field split and forge the
    # attach flag, defeating the attached-victim confirm and redirecting the kill.
    # A crafted `|`-name can't equal a real cc-N and is excluded by the anchor here;
    # the per-name `=`-exact query then can't shift field boundaries.
    mapfile -t names < <(tmux list-sessions -F '#{session_name}' 2>/dev/null \
        | grep -xE "${SESSION_PREFIX}-[0-9]+" || true)
    rows=()
    for nm in "${names[@]}"; do
        meta=$(tmux display-message -p -t "=${nm}" '#{session_attached}|#{t:session_activity}' 2>/dev/null || true)
        rows+=("${nm}|${meta}")
    done
    echo "" >&2
    echo "!  ${msg}" >&2
    if [ "${#rows[@]}" -eq 0 ]; then
        # No cc-N session to trade. Under a genuine OOM-floor breach, REFUSE the
        # new session — the swapless box is already below the safety floor from
        # NON-cc memory pressure, and spawning a ~3GB session risks an OOM that
        # takes down every session. This is the OOM circuit-breaker doing its
        # job, NOT the old collapse bug (which denied while sessions ran fine).
        # For a soft cap with nothing to reclaim, proceeding is harmless.
        if [ "$reason" = "oom_floor" ]; then
            echo "RAM is below the safety floor and no cc session exists to reclaim —" >&2
            echo "free non-cc memory and retry, or reattach an existing session." >&2
            exit 1
        fi
        echo "(no cc-N session to reclaim; proceeding.)" >&2
        return 0
    fi
    echo "" >&2
    for row in "${rows[@]}"; do
        IFS='|' read -r nm att act <<<"$row"
        state="detached"; [ "${att:-0}" -ge 1 ] && state="ATTACHED"
        echo "  [$i] ${nm}  ${state}  (idle since ${act})" >&2
        i=$((i + 1))
    done
    echo "" >&2
    if [ ! -t 0 ]; then
        echo "No interactive terminal here — reattach a session" >&2
        echo "(tmux attach -t ${SESSION_PREFIX}-<N>), or re-run 'ssh <host>-<N>' with a TTY to end one." >&2
        exit 1
    fi
    echo "Enter a number to END that session (frees memory + its slot; the transcript" >&2
    echo "persists — resume later with 'claude --resume'). Press Enter to cancel:" >&2
    IFS= read -r -p "> " choice </dev/tty || choice=""
    if [ -z "$choice" ]; then
        echo "Cancelled — reattach an existing session (tmux attach -t ${SESSION_PREFIX}-<N>)." >&2
        exit 1
    fi
    # Validate the choice fully BEFORE any arithmetic. Reject: leading zeros
    # (^[1-9][0-9]*$ — bash reads a leading-zero numeral as OCTAL → "08" errors),
    # AND an over-long digit string (the `-le 3` bound short-circuits before the
    # `-gt` comparison, so an oversized value like 2^64 can't raise an arithmetic
    # error that unwinds past the check and then WRAP the index to a valid slot,
    # killing the wrong session). The row list is always << 1000, so ≤3 digits
    # covers every real selection. 10# forces base-10 in the index defensively.
    if ! [[ "$choice" =~ ^[1-9][0-9]*$ ]] || [ "${#choice}" -gt 3 ] \
       || [ "$choice" -gt "${#rows[@]}" ]; then
        echo "Invalid selection — cancelled." >&2
        exit 1
    fi
    IFS='|' read -r victim victim_att _ <<<"${rows[$((10#$choice - 1))]}"
    # An ATTACHED session may have someone actively working in it — require an
    # explicit y/N before killing it (a detached slot needs no second prompt).
    if [ "${victim_att:-0}" -ge 1 ]; then
        echo "!  ${victim} is ATTACHED — someone may be using it. End it anyway? [y/N]" >&2
        IFS= read -r -p "> " _confirm </dev/tty || _confirm=""
        case "$_confirm" in
            y | Y | yes | YES) ;;
            *)
                echo "Cancelled — no session ended (reattach: tmux attach -t ${victim})." >&2
                exit 1
                ;;
        esac
    fi
    echo "Ending ${victim} to free room for ${SESSION_NAME}…" >&2
    # If the kill fails, NO memory was freed — do not spawn a new session on top
    # (that would over-commit the box, the very thing reclaim exists to prevent).
    if ! tmux kill-session -t "=${victim}" 2>/dev/null; then
        echo "Failed to end ${victim} — not starting a new session (no memory freed)." >&2
        echo "Reattach an existing session instead (tmux attach -t ${SESSION_PREFIX}-<N>)." >&2
        exit 1
    fi
    return 0
}

# Pure-bash fallback when the Python gate cannot run (broken venv / timeout).
# STATIC MemTotal cap — NEVER the collapsing free-RAM formula — + a hard OOM
# floor. Leans permissive so a Python outage never locks the operator out.
_cap_fail_open() {
    local mt ma total_mb avail_mb avail_known reserve per floor emerg cpus safe need
    local cg_max cg_cur cg_if cg_af cg_file head_mb is_op limit reason

    # Validate the operator levers BEFORE any arithmetic, with the SAME grammar as
    # the Python gate's CapConfig.from_env (so one ~/.genesis/cc-slot.env yields the
    # same cap on both paths). Positive levers: no leading zero (bash reads that as
    # OCTAL), 1-7 digits — an oversized value like 2^64 would otherwise WRAP to 0 in
    # $(( )) and divide-by-zero, aborting the login. Emergency: 0-99 (0 disables).
    # Anything malformed/oversized → the default.
    # `read -r <<<` strips surrounding whitespace exactly like Python's str.strip()
    # in CapConfig.from_env — so a padded value (e.g. "8192 ") is honored IDENTICALLY
    # on both paths, not accepted by the gate and silently defaulted here.
    read -r reserve <<<"${GENESIS_CC_SYSTEM_RESERVE_MB:-4096}"; [[ "$reserve" =~ ^[1-9][0-9]{0,6}$ ]] || reserve=4096
    read -r per     <<<"${GENESIS_CC_PER_SESSION_MB:-3072}";    [[ "$per"     =~ ^[1-9][0-9]{0,6}$ ]] || per=3072
    read -r floor   <<<"${GENESIS_CC_OOM_FLOOR_MB:-1536}";      [[ "$floor"   =~ ^[1-9][0-9]{0,6}$ ]] || floor=1536
    read -r emerg   <<<"${GENESIS_CC_EMERGENCY_SLOTS:-1}";      [[ "$emerg"   =~ ^(0|[1-9][0-9]?)$ ]] || emerg=1
    cpus=$(nproc 2>/dev/null || echo 1);                        [[ "$cpus"    =~ ^[1-9][0-9]{0,3}$ ]] || cpus=1

    # MemTotal / MemAvailable — validate each INDEPENDENTLY (an empty awk result
    # would break $(( )) under set -e, and an unreadable field must degrade only its
    # OWN gate, never both). MemTotal is required to size the cap; MemAvailable only
    # feeds the OOM floor, so a missing MemAvailable → skip the RAM gate, keep count.
    mt=$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo 2>/dev/null)
    if ! [[ "$mt" =~ ^[0-9]+$ ]]; then
        echo "cc-slot: cannot read MemTotal — capacity gate unavailable, allowing this slot." >&2
        return 0
    fi
    total_mb=$(( mt / 1024 ))
    ma=$(awk '/^MemAvailable:/{print $2; exit}' /proc/meminfo 2>/dev/null)
    if [[ "$ma" =~ ^[0-9]+$ ]]; then avail_mb=$(( ma / 1024 )); avail_known=1; else avail_mb=0; avail_known=0; fi

    # Cap by the container's cgroup v2 memory limit — procfs can expose HOST values
    # inside a container, which would size the cap for the host and trigger a cgroup
    # OOM (mirrors effective_memory()). v2 only here (Python handles v1); a v1 host
    # with the gate down uses procfs — the rare degraded case.
    if [ -r /sys/fs/cgroup/memory.max ]; then
        cg_max=$(cat /sys/fs/cgroup/memory.max 2>/dev/null)
        if [[ "$cg_max" =~ ^[0-9]+$ ]]; then
            [ $(( cg_max / 1048576 )) -lt "$total_mb" ] && total_mb=$(( cg_max / 1048576 ))
            cg_cur=$(cat /sys/fs/cgroup/memory.current 2>/dev/null)
            if [[ "$cg_cur" =~ ^[0-9]+$ ]]; then
                # Reclaimable = file LRU (inactive_file + active_file), NOT the `file`
                # counter — `file` also counts tmpfs/shmem, which live on the ANON LRU
                # and are NOT reclaimable for a new session (would over-state available
                # → wrongly ALLOW). Mirrors read_container_memory_reclaimable().
                cg_if=$(awk '/^inactive_file /{print $2; exit}' /sys/fs/cgroup/memory.stat 2>/dev/null); [[ "$cg_if" =~ ^[0-9]+$ ]] || cg_if=0
                cg_af=$(awk '/^active_file /{print $2; exit}' /sys/fs/cgroup/memory.stat 2>/dev/null);   [[ "$cg_af" =~ ^[0-9]+$ ]] || cg_af=0
                cg_file=$(( cg_if + cg_af ))
                head_mb=$(( (cg_max - cg_cur + cg_file) / 1048576 )); [ "$head_mb" -lt 0 ] && head_mb=0
                if [ "$avail_known" = 1 ]; then
                    [ "$head_mb" -lt "$avail_mb" ] && avail_mb=$head_mb
                else
                    avail_mb=$head_mb; avail_known=1
                fi
            else
                # cgroup limit known but usage unreadable → procfs MemAvailable may be
                # HOST headroom (over-allow on a swapless container). Estimate free from
                # the capacity model instead (mirror the Python degrade): total − reserve
                # − existing×per. Never trust host free RAM here. (User decision 2026-08-27.)
                avail_mb=$(( total_mb - reserve - existing * per )); [ "$avail_mb" -lt 0 ] && avail_mb=0
                avail_known=1
            fi
        fi
    fi

    safe=$(( (total_mb - reserve) / per )); [ "$safe" -lt 1 ] && safe=1
    [ "$safe" -gt "$cpus" ] && safe=$cpus   # nproc clamp (process-aware; thrash guard)
    # Room to START one more session must cover a FULL per-session footprint (a
    # session grows toward `per`), plus the absolute floor — whichever is larger.
    need=$per; [ "$floor" -gt "$need" ] && need=$floor

    # Origin: ANY SSH login ($SSH_CONNECTION set) is the OPERATOR — emergency slot +
    # interactive reclaim, never a hard deny (user policy 2026-08-27: SSH = operator).
    # No SSH_CONNECTION is the "normal method" (dashboard web terminal / local
    # console) — held to `safe`, plain deny. This static path can't classify the
    # client IP without a hand-rolled parser (the Codex tar-pit), so it keys on SSH
    # PRESENCE; documented fail-open-only divergence: a public-IP SSH gets the
    # operator affordance here, whereas the Python gate would DENY it.
    if [ -n "${SSH_CONNECTION:-}" ]; then is_op=1; limit=$(( safe + emerg )); else is_op=0; limit=$safe; fi

    # ALLOW iff under the origin's limit AND (RAM unknown OR enough headroom).
    if [ "$existing" -lt "$limit" ] && { [ "$avail_known" = 0 ] || [ "$avail_mb" -ge "$need" ]; }; then
        if [ "$avail_known" = 0 ]; then
            echo "cc-slot: capacity gate unavailable, MemAvailable unreadable — count-cap only ($(( existing + 1 ))/${safe})." >&2
        else
            echo "cc-slot: capacity gate unavailable — static fallback allows this slot ($(( existing + 1 ))/${safe})." >&2
        fi
        return 0
    fi

    # Not allowed → classify reason (RAM tight vs at-limit).
    if [ "$avail_known" = 1 ] && [ "$avail_mb" -lt "$need" ]; then reason="oom_floor"; else reason="cap_full"; fi

    if [ "$is_op" = 1 ]; then
        # Operator (SSH): never a hard no — interactive reclaim (pick a session to end).
        if [ "$reason" = "oom_floor" ]; then
            _cap_reclaim "Capacity gate unavailable; RAM low (${avail_mb}MB free, need >= ${need}MB)." "$reason"
        else
            _cap_reclaim "Capacity gate unavailable; at the limit (${existing}/${safe}+${emerg})." "$reason"
        fi
        return 0
    fi
    # Normal method (dashboard/local console): plain deny, message matches the reason.
    if [ "$reason" = "oom_floor" ]; then
        echo "ERROR: RAM low (${avail_mb}MB free, need >= ${need}MB) [capacity gate unavailable]." >&2
    else
        echo "ERROR: Session cap reached (${existing}/${safe}) [capacity gate unavailable]." >&2
    fi
    _cap_list_slots
    exit 1
}

# Numeric slots only: retired cc-manual-<ts>-<pid> sessions from the old wrapper
# (and any other cc-* stray) must not consume cap headroom — manual allocation
# can only ever probe/create cc-<N>. This count is a point-in-time snapshot: two
# logins racing at the same instant can both read the same `existing` and both
# spawn (a benign, pre-existing over-count of at most the concurrency — the cap is
# a resource governor, not a lock; the OOM floor still guards actual exhaustion).
existing=$(tmux list-sessions -F '#{session_name}' 2>/dev/null \
           | grep -cE "^${SESSION_PREFIX}-[0-9]+$" || true)

# Reattaching to existing session — always allow ('=' = exact-name match)
_SESSION_EXISTS=0
_HEAL=0
_HEAL_PANE=""
if tmux has-session -t "=$SESSION_NAME" 2>/dev/null; then
    _SESSION_EXISTS=1  # bypass the CAP gate: attaching adds no session, so the
                       # slot's RAM footprint is already counted. It no longer
                       # implies skipping the OAuth gate — see the _HEAL branch,
                       # which starts a FRESH claude and therefore needs the same
                       # token treatment a create does.
    #
    # Name existence is NOT liveness. `tmux new-session -A` ATTACHES to an
    # existing session and silently DISCARDS the shell-command argument, so a
    # slot sitting at a bare shell prompt stays that way forever: every later
    # connection re-attaches to the same prompt and the launch command never
    # runs. Probe for a real claude and relaunch into the pane if there is none.
    #
    # `list-panes -s` covers EVERY window (a slot grows windows via Ctrl-b c)
    # and lists them in window index order, so the first row is the pane of the
    # LOWEST SURVIVING window index — where the canonical launch lives, and the
    # right heal target. (Not necessarily window 1: if window 1 was killed the
    # first row is window 2's pane.)
    _panes=$(tmux list-panes -s -t "=$SESSION_NAME" \
        -F '#{pane_id} #{pane_pid} #{pane_current_command}' 2>/dev/null || true)
    if [ -n "$_panes" ]; then
        _first_pane=$(printf '%s\n' "$_panes" | head -1 | cut -d' ' -f1)
        _pane_pids=$(printf '%s\n' "$_panes" | cut -d' ' -f2 | tr '\n' ' ')
        # The verdict is delegated to a pure, unit-tested helper. It reads
        # /proc for an interactive claude descending from any pane pid —
        # NOT `#{pane_current_command}`, which reports "bash" for the canonical
        # `bash -c "… claude …; trailer"` pane while claude IS running (no job
        # control, so tmux resolves the fg group to the shell). Keying on that
        # would classify a healthy slot as broken and type into a live session.
        _live_out=$(_probe_liveness $_pane_pids)
        # Fail toward SPARING: only an explicit POISONED heals. Anything else —
        # ALIVE, UNKNOWN, a broken venv, a timeout, an empty read — attaches,
        # which is the pre-existing behaviour and costs nothing. The opposite
        # error types a command into a running session.
        # A pane id is `%<digits>`. stderr is discarded above, so any stray
        # stdout would otherwise become a send-keys target.
        case "$_first_pane" in
            %[0-9]*) ;;
            *) _first_pane="" ;;
        esac
        # Nobody may be sitting at this shell. Child-presence answers "is a
        # PROCESS running here" and cannot see shell-NATIVE work: `source
        # deploy.sh`, a `read` waiting on input, a loop of builtins — all run
        # inside the pane's own shell, spawn no child, and report `bash`. The
        # probe calls that IDLE and the door would then C-c a human's task.
        #
        # Attachment is a CLOSED-SET fact rather than another heuristic, and it
        # is the right question: is anyone there. The door heals BEFORE it
        # attaches, so at this moment a non-zero count means some OTHER client
        # is already in the session — a person who may be mid-keystroke.
        # Accepted cost, stated rather than hidden: a poisoned slot held open
        # in a second window will not self-heal until that window closes. That
        # is a stall; interrupting someone's deploy is damage.
        _attached=$(tmux display-message -p -t "=$SESSION_NAME" '#{session_attached}' 2>/dev/null || echo "")
        # Fail toward SPARING, like every other gate here: only an affirmative
        # "nobody is attached" permits keystrokes. Coercing an empty or
        # non-numeric answer to 0 would read a BROKEN query as "nobody there"
        # and type anyway — caught by test, which asked an unreadable count to
        # spare the session and watched it heal instead.
        case "$_attached" in
            ''|*[!0-9]*) _attached=1 ;;
        esac
        if [ "$_live_out" = "POISONED" ] && [ -n "$_first_pane" ] && [ "$_attached" -gt 0 ]; then
            echo "cc-slot: slot ${SLOT} is running no claude, but another client is attached — not typing into a session someone may be using." >&2
            echo "cc-slot: close the other window and reconnect to relaunch it." >&2
        fi
        if [ "$_live_out" = "POISONED" ] && [ -n "$_first_pane" ] && [ "$_attached" -eq 0 ]; then
            _HEAL=1
            _HEAL_PANE="$_first_pane"
            # A heal is create-like for MEMORY, not just for the OAuth gate.
            # The bypass above is justified for an ATTACH — it adds no session,
            # so the slot's footprint is already counted. That reasoning does
            # NOT carry to a heal: this slot is poisoned precisely because no
            # claude is running in it, so healing STARTS one. On a swapless box
            # that is a real allocation, and the capacity gate exists to decide
            # whether the box can take it.
            #
            # `--existing $((existing - 1))` and not `$existing`: for a CREATE
            # the count excludes the session about to be made, so passing the
            # raw count would judge a heal one session busier than the create
            # it is modelled on — and at the cap that means a poisoned slot can
            # NEVER be healed, stranding the operator on the one slot that is
            # broken. Excluding this slot asks the question the gate is for:
            # "may one more claude start right now", given this one is not
            # currently running.
            #
            # ONLY an explicit DENY stands the heal down, and it never exits:
            # the operator still gets their shell, exactly as before this
            # feature existed. A gate that is unavailable, slow or unparseable
            # heals — same fail-open direction the create path takes, for the
            # same reason (an unreachable gate must not become an outage).
            _heal_cap_py="${GENESIS_ROOT}/.venv/bin/python"
            if [ -x "$_heal_cap_py" ]; then
                # Clamped: `existing` is a counted value and nothing
                # guarantees it is >= 1 here (a listing that raced the session
                # into being, or a count that excludes it). A negative would
                # reach the gate as a nonsense population — caught by test,
                # which asked for `--existing 0` and got `-1`.
                _heal_existing=$(( existing > 0 ? existing - 1 : 0 ))
                _heal_cap_out=$(timeout 15 "$_heal_cap_py" \
                    -m genesis.cc.session_cap --existing "$_heal_existing" \
                    2>/dev/null || true)
                if [ "$(printf '%s\n' "$_heal_cap_out" | sed -n '1p')" = "DENY" ]; then
                    _HEAL=0
                    _HEAL_PANE=""
                    echo "cc-slot: slot ${SLOT} has no claude running, but the capacity gate declined to start one: $(printf '%s\n' "$_heal_cap_out" | sed -n '2p')" >&2
                    echo "cc-slot: attaching to the slot's shell instead — free a slot and reconnect to relaunch." >&2
                fi
            fi
        fi
    fi
else
    # New session → consult the capacity gate (SSH_CONNECTION classifies origin
    # inside the Python helper). `timeout` bounds a hung import; trailing
    # `|| true` keeps a non-zero exit from aborting under `set -e`.
    _cap_py="${GENESIS_ROOT}/.venv/bin/python"
    _cap_out=""
    if [ -x "$_cap_py" ]; then
        _cap_out=$(timeout 15 "$_cap_py" -m genesis.cc.session_cap --existing "$existing" 2>/dev/null || true)
    fi
    # Protocol: line 1 = action, line 2 = human message, line 3 = machine reason.
    _cap_action=$(printf '%s\n' "$_cap_out" | sed -n '1p')
    _cap_msg=$(printf '%s\n' "$_cap_out" | sed -n '2p')
    _cap_reason=$(printf '%s\n' "$_cap_out" | sed -n '3p')
    case "$_cap_action" in
        ALLOW)   [ -n "$_cap_msg" ] && echo "cc-slot: ${_cap_msg}" >&2 || true ;;
        DENY)    echo "ERROR: ${_cap_msg}" >&2; _cap_list_slots; exit 1 ;;
        RECLAIM) _cap_reclaim "$_cap_msg" "$_cap_reason" ;;
        *)       _cap_fail_open ;;   # empty/unexpected → Python gate unavailable
    esac
fi

# `live: N` = current numeric cc-N count (excludes retired cc-manual-* strays);
# the gate/fallback message above carries the cap itself.
echo "→ Slot ${SLOT} (session: ${SESSION_NAME}, live: ${existing})" >&2

# Redirect CC temp to dedicated directory (keeps /tmp clean)
export TMPDIR="$HOME/.genesis/cc-tmp"
mkdir -p "$TMPDIR"
chmod 700 "$TMPDIR"

# Move CC's Bash sandbox off volatile /tmp onto persistent disk.
# CC uses CLAUDE_CODE_TMPDIR for its sandbox root (/claude-<uid>/<cwd>/).
# Without this, intermittent ENOENT failures on /tmp break the Bash tool.
export CLAUDE_CODE_TMPDIR="$HOME/.genesis/cc-tmp"

# Permission mode for this interactive dev console. Default: auto — auto-approves
# common ops but still prompts on deny/ask rules, which the operator answers in
# the tmux session (keeps deny-rule safety). To launch friction-free with
# --dangerously-skip-permissions, set GENESIS_CC_PERMISSION_MODE=bypass. SSH
# RemoteCommand does not source .bashrc, so this script also reads an optional
# ~/.genesis/cc-slot.env (e.g. a single line: GENESIS_CC_PERMISSION_MODE=bypass).
# That file is also where the OAuth-durability lever lives:
# GENESIS_CC_SLOT_OAUTH=conditional (default) | always | off — set `off` for a
# slot that must keep Remote Control / claude.ai connectors even after the login
# dies (see the OAuth block below).
# Headless/autonomous CC sessions (CCInvoker -p) keep bypass separately — no
# human is present to answer a prompt.
# (~/.genesis/cc-slot.env is already sourced near the top, before the capacity
# gate, so these levers are populated here.)
case "${GENESIS_CC_PERMISSION_MODE:-auto}" in
    bypass|dangerous|skip) CC_PERM_FLAG="--dangerously-skip-permissions" ;;
    *)                     CC_PERM_FLAG="--permission-mode auto" ;;
esac

# Forwarded manual-mode args: shell-quote each one (%q) — the command below is
# a single string tmux hands to the default shell (bash on Genesis installs).
# A caller-supplied permission flag suppresses CC_PERM_FLAG so claude never
# receives two conflicting permission arguments.
CLAUDE_ARGS_Q=""
_HAS_BARE=0
if [[ ${#CLAUDE_EXTRA_ARGS[@]} -gt 0 ]]; then
    for arg in "${CLAUDE_EXTRA_ARGS[@]}"; do
        case "$arg" in
            --dangerously-skip-permissions|--permission-mode|--permission-mode=*)
                CC_PERM_FLAG="" ;;
        esac
        # --bare ignores CLAUDE_CODE_OAUTH_TOKEN (CC auth precedence), so
        # injecting the setup-token would be inert — skip the OAuth gate below.
        [ "$arg" = "--bare" ] && _HAS_BARE=1
    done
    CLAUDE_ARGS_Q=$(printf ' %q' "${CLAUDE_EXTRA_ARGS[@]}")
fi

# --- OAuth login durability (login-dead-conditional; WS-1) -------------------
# On slot CREATE only: if the interactive /login is dead, continue this pane on
# the stored 1-year setup-token so the session survives without a re-login
# prompt. genesis.cc.login_gate makes the decision (reusing login_health's
# shared login-dead gate — one authority, same as CCInvoker); the token itself
# is read INSIDE the pane shell (never in any argv/ps) via a conditional prefix
# on the command string. Lever GENESIS_CC_SLOT_OAUTH=conditional(default)|always|off.
# cc-slot.sh is a registered reader of cc_oauth_token.env — see
# docs/architecture/shared-artifacts.md.
_OAUTH_SRC=""
_slot_oauth_mode="${GENESIS_CC_SLOT_OAUTH:-conditional}"
_slot_oauth_mode="${_slot_oauth_mode,,}"   # normalize; the gate is the value authority
if { [ "$_SESSION_EXISTS" = "0" ] || [ "$_HEAL" = "1" ]; } \
   && [ "$_slot_oauth_mode" != "off" ] && [ "$_HAS_BARE" = "0" ]; then
    # The gate both DECIDES and AUTHORS the notice: on inject it exits 0 and
    # prints the mode-appropriate human notice to stdout — captured here so the
    # notice text lives in ONE place (no bash/gate divergence), and so lever
    # semantics (unknown value → fail-closed, peer-route/override exclusion,
    # stale-token exclusion) are enforced solely in genesis.cc.login_gate, a
    # faithful mirror of CCInvoker's fallback contract. `if …; then` keeps a
    # non-zero exit from aborting the launch under `set -e`; `timeout 30` bounds
    # a hung probe. `env` hands the resolved mode across (a plain
    # `GENESIS_CC_SLOT_OAUTH=always` in ~/.genesis/cc-slot.env is a non-exported
    # shell var the subprocess would otherwise never see).
    if _oauth_notice=$(timeout 30 env GENESIS_CC_SLOT_OAUTH="$_slot_oauth_mode" \
            "${GENESIS_ROOT}/.venv/bin/python" -m genesis.cc.login_gate); then
        # %q so the notice cannot break out of the pane command string (any
        # future notice edit is injection-proof by construction, not by luck).
        _notice_q=$(printf '%q' "$_oauth_notice")
        # Runs in the PANE shell: read the token with the SAME parser the gate
        # used (login_health.read_fallback_token — no sed/parser divergence),
        # export it ONLY when non-empty (a failed/empty read never exports a
        # blank credential), then echo the notice to stderr. The token flows
        # python-stdout → $(...) → a shell var → the process ENV, never any argv
        # (no ps/scrollback leak). `$(...)`, `\$`, and the literal single-quoted
        # python defer to the pane shell; ${GENESIS_ROOT}/${_notice_q} expand here.
        _OAUTH_SRC="_gt=\"\$(\"${GENESIS_ROOT}/.venv/bin/python\" -c 'import sys; from genesis.cc.login_health import read_fallback_token as r; sys.stdout.write(r() or str())' 2>/dev/null)\"; if [ -n \"\$_gt\" ]; then export CLAUDE_CODE_OAUTH_TOKEN=\"\$_gt\"; printf '%s\\n' ${_notice_q} >&2; fi; unset _gt; "
    fi
fi

# -u: force UTF-8 output even if a future client's locale detection fails.
#
# The inner command drops the old `exec claude` so that when claude EXITS we can
# record why before the pane vanishes: cc_exit_capture.sh logs the exit status
# (signal-decoded) + a pane-scrollback tail to ~/.genesis/logs/cc_exit_<slot>.log.
# Without this a dying session (V8 abort, OOM kill, clean exit) leaves no trace —
# the 2026-08-19 death was undiagnosable for exactly that reason. `exit $__ec`
# reproduces claude's exit code as the pane's, so tmux sees the same dead-status
# and the `-A` attach-or-create behaviour (this runs only on CREATE) is unchanged.
# Captures when claude EXITS on its own (crash/OOM-of-claude/clean quit — the
# cases we care about); a SIGHUP to the pane itself (tmux kill-session / unit
# stop) may reap the wrapper before the trailer runs — the watchgod OOM sampler
# covers that subset. Capture is best-effort and never alters the exit code.
# `\$` defers expansion to the pane's shell; the `${...}` expand here in cc-slot.sh.
# The token-prep prefix `${_OAUTH_SRC}` goes BEFORE `cd` so the original
# `cd $ROOT && claude` guard stays intact: `_OAUTH_SRC` ends in `;`, so placing it
# after the `&&` (`cd $ROOT && <prefix>; claude`) would bind the `&&` to the prefix
# only and launch claude even when cd fails. The prefix is cwd-independent (absolute
# python path, home-anchored token file), and if the repo is gone that python is too
# → the read silently no-ops before cd fails. Do NOT move it after the `&&`
# (test_cd_guard_skips_claude_on_bad_cd).
# ONE builder for the pane command. The create path hands it to tmux and the
# heal path types it into an existing pane — they must never drift, or a healed
# slot would run a different claude (no OAuth prefix, no permission flag, no
# exit-capture trailer) and would re-poison itself on the next exit.
#
# `command claude`, not bare `claude`, and that word is load-bearing: an
# identical STRING is not identical EXECUTION. tmux runs the create path under a
# non-interactive `sh -c` with no rc files, whereas the heal path is typed into
# an INTERACTIVE shell that has sourced ~/.bashrc — where `claude` resolves to
# the bashrc wrapper FUNCTION, which would re-derive the environment itself and
# invoke cc_exit_capture a second time. MEASURED: the function does interpose.
# `command` bypasses it, so both paths run the same binary with the same argv.
# `\$` defers to the pane shell and resolves identically under tmux's `sh -c`
# and an interactive bash.
_PANE_CMD="${_OAUTH_SRC}cd ${GENESIS_ROOT} && command claude ${CC_PERM_FLAG}${CLAUDE_ARGS_Q}; __ec=\$?; ${GENESIS_ROOT}/scripts/cc_exit_capture.sh ${SLOT} \$__ec >/dev/null 2>&1; exit \$__ec"

# ONE env source for both paths. `tmux new-session -e` seeds a FRESH pane's
# environment; a healed pane keeps whatever its old shell had, so the heal
# types the SAME set as `export`s ahead of the command — otherwise a healed
# claude runs with a stale GENESIS_SLOT / permission mode / TMPDIR, the exact
# create-vs-heal drift the single _PANE_CMD builder exists to prevent. LANG
# stays LAST: a doors test parses the create line from its final -e.
_PANE_ENV=(
    "GENESIS_SLOT=${SLOT}"
    "GENESIS_CC_PERMISSION_MODE=${GENESIS_CC_PERMISSION_MODE:-auto}"
    "CLAUDE_CODE_TMPDIR=${CLAUDE_CODE_TMPDIR}"
    # TMPDIR must be passed EXPLICITLY, and on BOTH paths. The obvious story —
    # "create inherits it from this process" — is FALSE, and measurement says
    # so: `tmux new-session` runs the pane from the tmux SERVER's environment,
    # not the invoking client's, so a server started with a different TMPDIR
    # wins (MEASURED: pane got the server's value without `-e`, the door's with
    # it). So the create path never reliably inherited it either, and any slot
    # created after a server someone else started got the ambient value —
    # plausibly /tmp, which this repo treats as a small tmpfs to stay off. A
    # heal into an already-running shell cannot inherit it at all. Same reason
    # CLAUDE_CODE_TMPDIR has always been in this list.
    "TMPDIR=${TMPDIR}"
    "LANG=${LANG:-}"
)
_PANE_ENV_FLAGS=()
_HEAL_ENV_EXPORTS=""
for _kv in "${_PANE_ENV[@]}"; do
    _PANE_ENV_FLAGS+=(-e "$_kv")
    _HEAL_ENV_EXPORTS+="export $(printf '%q' "$_kv"); "
done

if [ "$_HEAL" = "1" ]; then
    # SERIALIZE per slot. Every door converges here, so two of them (the
    # dashboard web terminal and an SSH login, or an SSH retry) can hold the
    # same POISONED verdict concurrently, both heal, and the second one type
    # into the claude the first just started. A read cannot claim; this can.
    # Best-effort: a missing flock or an unwritable lock path must not stop a
    # legitimate heal.
    # NOTE the shape: `exec` applies its redirections to the SHELL, so a
    # `2>/dev/null` on the exec line would silence this script's stderr for the
    # REST OF THE RUN — every later notice, including the heal message, would
    # vanish while the heal itself still happened. Probe writability with an
    # ordinary command first (safe to silence), and exec only once it is known
    # to succeed.
    _lock="${HOME}/.genesis/cc-slot-heal-${SLOT}.lock"
    _LOCK_HELD=0
    if command -v flock >/dev/null 2>&1 \
       && mkdir -p "${HOME}/.genesis" 2>/dev/null \
       && : >>"$_lock" 2>/dev/null; then
        exec 9>>"$_lock"
        _LOCK_HELD=1
        flock -w 45 9 2>/dev/null || true
    fi

    # FRESH pane state — everything below reads CURRENT reality, not the
    # snapshot the heal was DECIDED on. The OAuth gate is allowed up to 30s
    # between decision and keystrokes, ample for the operator to have started
    # vim in the pane (or killed the window); a gate that consults the old
    # snapshot approves typing into a pane it never looked at.
    _panes_now=$(tmux list-panes -s -t "=$SESSION_NAME" \
        -F '#{pane_id} #{pane_pid} #{pane_current_command}' 2>/dev/null || true)
    _fresh_row=$(printf '%s\n' "$_panes_now" | head -1)
    _fresh_pane=${_fresh_row%% *}
    _fresh_pid=$(printf '%s\n' "$_fresh_row" | cut -d' ' -f2)
    # Full remainder after id+pid, NOT field 3: a process name containing a
    # space ("bash x") must not pass the whitelist on its first token. A row
    # with no third field leaves the whole row here, which no shell name
    # matches — the sparing direction.
    _fresh_cmd=${_fresh_row#* * }
    _pane_pids_now=$(printf '%s\n' "$_panes_now" | cut -d' ' -f2 | tr '\n' ' ')
    if [ -z "$_panes_now" ] || [ "$_fresh_pane" != "$_HEAL_PANE" ]; then
        # Gone, or the first window's pane is no longer the one we probed —
        # whatever sits there now was never blessed by any verdict.
        echo "cc-slot: ${SESSION_NAME}'s pane changed while preparing — attaching instead." >&2
        _HEAL=0
    fi
fi

if [ "$_HEAL" = "1" ]; then
    # SAFETY GATE — asked BEFORE any keystroke, including the C-c.
    # The liveness probe answers "is claude running here"; it does NOT answer
    # "is it safe to type here". MEASURED: send-keys C-c KILLS a running
    # foreground job, so a pane with no claude but an active build, ssh session
    # or editor must be left strictly alone.
    #
    # Two complementary signals, each covering the other's blind spot; BOTH
    # must approve. `#{pane_current_command}` catches a pane whose process is
    # not a shell at all (an exec'd vim has no children and would pass the
    # child check). Child-presence catches a busy SHELL: `bash script.sh`,
    # rsync, an editor — all children of the pane process, all reported merely
    # as their leader's name here. Error directions stay harmless: a false
    # "bash"/IDLE only ever permits a heal the liveness probe already blessed
    # twice, and a false BUSY only ever suppresses one.
    # No fish: the typed payload (`export K=V;`, `__ec=$?`) is POSIX and a
    # parse error there — a fish pane gets a plain attach instead of a broken
    # relaunch. dash/ksh/zsh parse it fine.
    case "$_fresh_cmd" in
        bash | -bash | sh | -sh | zsh | -zsh | dash | ksh) ;;
        *)
            echo "cc-slot: ${SESSION_NAME} has no claude, but its pane is running '${_fresh_cmd}' — attaching without touching it." >&2
            _HEAL=0
            ;;
    esac
fi

if [ "$_HEAL" = "1" ]; then
    # RE-CHECK liveness against the FRESH pane pids. Typing into a live Claude Code TUI is the one failure this
    # whole change must never cause, so the verdict is confirmed HERE, and
    # anything other than a still-POISONED answer stands down. (This also
    # closes the released-lock race: a second door serialized behind the flock
    # re-runs these gates and finds the first door's claude as a fresh child.
    # ACCEPTED RESIDUAL: a few ms between door 1's Enter and its bash forking
    # claude, versus door 2's ~100ms of python probe spawns — both probes
    # landing inside that window needs scheduler starvation of the pane shell;
    # not closable without tmux-side atomicity.)
    _recheck=$(_probe_liveness $_pane_pids_now)
    if [ "$_recheck" != "POISONED" ]; then
        echo "cc-slot: ${SESSION_NAME} came alive while preparing — attaching instead." >&2
        _HEAL=0
    fi
fi

if [ "$_HEAL" = "1" ]; then
    # IDLENESS IS THE LAST GATE, and that position is the whole point: it is
    # the DESTRUCTIVE question ("may I type here", and the first keystroke is a
    # C-c that kills a running foreground job), so its answer must be the
    # freshest one taken. Asked before the liveness re-probe it went stale by
    # that probe's whole timeout (up to 15s) — long enough for the operator to
    # start a build in the pane and have it interrupted. Same stale-verdict
    # defect as reading the pane snapshot, one gate further in.
    _idle=$(_probe_liveness --idle "$_fresh_pid")
    if [ "$_idle" != "IDLE" ]; then
        if [ "$_idle" = "BUSY" ]; then
            echo "cc-slot: ${SESSION_NAME} has no claude, but its shell is mid-job — attaching without touching it." >&2
        else
            echo "cc-slot: ${SESSION_NAME} has no claude, but its idleness could not be established — attaching without touching it." >&2
        fi
        _HEAL=0
    fi
fi

if [ "$_HEAL" = "1" ]; then
    echo "cc-slot: ${SESSION_NAME} is running no claude — relaunching it in the existing pane." >&2
    # C-c interrupts a stray foreground process and abandons a half-typed line;
    # the brief pause lets the shell reprint its prompt (C-c also flushes the
    # tty input queue); C-u clears anything left on it. `-l` sends the payload
    # literally so no character is interpreted as a key name.
    #
    # Every send is CHECKED: an undeliverable keystroke (pane died, server
    # gone) must not be shrugged past, or the door claims a heal that never
    # reached the pane and the operator attaches expecting claude. On the
    # first failure no further keys are sent (a best-effort C-u sweeps any
    # half-typed payload) and the failure is said out loud.
    _heal_sent=1
    tmux send-keys -t "$_HEAL_PANE" C-c 2>/dev/null || _heal_sent=0
    if [ "$_heal_sent" = "1" ]; then
        sleep 0.3 || true
        tmux send-keys -t "$_HEAL_PANE" C-u 2>/dev/null || _heal_sent=0
    fi
    if [ "$_heal_sent" = "1" ]; then
        tmux send-keys -t "$_HEAL_PANE" -l "${_HEAL_ENV_EXPORTS}${_PANE_CMD}" 2>/dev/null || _heal_sent=0
    fi
    if [ "$_heal_sent" = "1" ]; then
        tmux send-keys -t "$_HEAL_PANE" Enter 2>/dev/null || _heal_sent=0
    fi
    if [ "$_heal_sent" != "1" ]; then
        tmux send-keys -t "$_HEAL_PANE" C-u 2>/dev/null || true
        echo "cc-slot: heal keystrokes could not be delivered — attaching without a relaunch." >&2
    fi
fi

# The heal lock's fd has no FD_CLOEXEC, and `exec tmux` replaces this shell —
# an open fd 9 would ride into the attached CLIENT and hold the per-slot flock
# for the whole session, stalling every later door's `flock -w 45` for
# nothing. The serialization the lock exists for (two doors healing the same
# pane) ends at the keystrokes above, so release it here.
if [ "${_LOCK_HELD:-0}" = "1" ]; then
    exec 9>&-
fi

# Exactly ONE new-session on every path (create, alive-attach, heal-attach).
# On an attach `-A` discards the command argument harmlessly; the heal above is
# purely a prelude to it.
exec tmux -u new-session -A -s "$SESSION_NAME" \
    "${_PANE_ENV_FLAGS[@]}" \
    "$_PANE_CMD"
