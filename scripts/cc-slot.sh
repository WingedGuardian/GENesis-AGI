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
# disagreeing: the manual-mode slot map, the heal decision, and the RE-CHECK
# taken under the lock. There is no longer a `--idle` companion question: the
# safe-to-destroy decision is the operator's, taken at the confirm. Defined
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
_cap_warn=""
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
        # DETECTION ONLY. Whether anyone is USING this pane is asked of the
        # operator at the point of action (see the confirm before the respawn),
        # never inferred here.
        #
        # It was inferred here, across four review rounds and eight findings —
        # idleness, attachment, capacity, freshness — and each round found the
        # previous round's gate incomplete. That is not carelessness; the
        # question is UNDECIDABLE from outside the pane. `source deploy.sh`, a
        # loop of builtins and a bare idle prompt all spawn no child, burn no
        # distinguishing CPU, and share the shell's own foreground process
        # group. There is no signal left to read.
        #
        # MEASURED, and the sharpest evidence that stacking gates was the wrong
        # answer: the capacity gate added in round 3 tested only for `DENY`,
        # but `session_cap.decide()` returns ALLOW or RECLAIM for an operator
        # and never DENY (src/genesis/cc/session_cap.py). On the SSH path the
        # gate could not fire at all — a guard that reviewed clean twice and
        # was dead code the whole time.
        #
        # A person at an SSH prompt can see their own pane. Asking them is not
        # a weaker answer than the heuristics; it is the only correct one.
        if [ "$_live_out" = "POISONED" ] && [ -n "$_first_pane" ]; then
            _HEAL=1
            _HEAL_PANE="$_first_pane"
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
    # PATH is pinned for the same reason, and it is why the ACTION changed.
    # `respawn-pane` runs the command from the tmux SERVER's environment, which
    # is correct only by luck of who started the server; the typed heal it
    # replaced inherited the STALE PANE SHELL's PATH instead, which is worse.
    # `_PANE_CMD` resolves `command claude` through PATH, so neither ambient
    # value may be trusted. Pinning it here means the create and heal paths
    # resolve the same binary from one source.
    "PATH=${PATH}"
    "LANG=${LANG:-}"
)
_PANE_ENV_FLAGS=()
for _kv in "${_PANE_ENV[@]}"; do
    _PANE_ENV_FLAGS+=(-e "$_kv")
done

# SERIALIZE per slot, around the DESTRUCTIVE ACTION ONLY — never around the
# prompt. Two doors can hold the same POISONED verdict concurrently (an SSH
# login and the dashboard terminal, or an SSH retry) and both try to rebuild.
# MEASURED cost of getting the scope wrong: with the confirm inside the lock a
# second door sat silent for the full `flock -w 45` while the first waited on a
# human, and past that timeout the `|| true` drops serialization entirely so
# BOTH could respawn. The lock now spans only re-validate-and-respawn, which is
# bounded by probe timeouts.
# NOTE the shape: `exec` applies its redirections to the SHELL, so a
# `2>/dev/null` on the exec line would silence this script's stderr for the
# REST OF THE RUN. Probe writability with an ordinary command first (safe to
# silence), and exec only once it is known to succeed.
# ── WHAT THE DESTRUCTIVE ACTION DEPENDS ON ─────────────────────────────────
# `respawn-pane -k` kills a process group, and the operator's answer sits an
# unbounded time before it. Five review rounds each found a DIFFERENT input
# that was read before that pause and used after it. Patching them one at a
# time is what produced those five rounds, so the set is ENUMERATED here and
# every member is re-derived in the final gate:
#
#   1. the pane exists and is the one that was probed   _revalidate_heal
#   2. no claude is running in it                       _revalidate_heal
#   3. it still runs what the operator was SHOWN        _disclosed_cmd compare
#   4. the session still exists                         checked before the exec
#   5. the box can afford another claude                _capacity_permits
#   6. the pane environment is set                      set-environment, guarded
#   7. the per-slot lock is held                        _acquire_heal_lock
#
# Immune BY CONSTRUCTION, listed so this is a complete set rather than a
# convenient one: the OAuth token is read at pane-exec time by `_OAUTH_SRC`, so
# it cannot go stale across the pause; PATH, TMPDIR, the permission mode and
# LANG come from this process's own environment and do not vary with time.
#
# If a review finds an eighth, this enumeration was wrong and the mechanism —
# not the instance — is what needs to change.
_acquire_heal_lock() {
    _lock="${HOME}/.genesis/cc-slot-heal-${SLOT}.lock"
    _LOCK_HELD=0
    if command -v flock >/dev/null 2>&1 \
       && mkdir -p "${HOME}/.genesis" 2>/dev/null \
       && : >>"$_lock" 2>/dev/null; then
        exec 9>>"$_lock"
        _LOCK_HELD=1
        flock -w 45 9 2>/dev/null || true
    fi
}

# Re-read the pane and re-confirm POISONED from CURRENT reality. Called TWICE
# BY DESIGN: once to build an honest disclosure for the prompt, and again under
# the lock immediately before the respawn.
#
# The SECOND call is the load-bearing one. An operator takes unbounded time at
# a prompt, and in that time they can attach from another terminal and start
# claude by hand — precisely what the bootstrap in-tmux branch exists for. A
# verdict taken BEFORE the prompt and acted on AFTER it is the same
# decision-time/action-time defect this door has already shipped twice; moving
# the decision to a human does not retire that defect, it lengthens its window.
# Capacity, as a FRESH verdict on every call. The session count is recounted
# here rather than reused from door entry: a count taken once and judged later
# is the same stale-input defect as every other member of the list above.
# Sets _cap_action / _cap_msg / _cap_reason; an empty action means no verdict.
_capacity_verdict() {
    local live n out
    _cap_action=""; _cap_msg=""; _cap_reason=""
    [ -x "${GENESIS_ROOT}/.venv/bin/python" ] || return 0
    live=$(tmux list-sessions -F '#{session_name}' 2>/dev/null \
           | grep -cE "^${SESSION_PREFIX}-[0-9]+$" || true)
    # A rebuild is create-like for MEMORY, and a create's count excludes the
    # session about to exist — so ask about one fewer. Clamped: a negative
    # reaches the gate as a nonsense population (caught by an earlier round).
    n=$(( live > 0 ? live - 1 : 0 ))
    out=$(timeout 15 "${GENESIS_ROOT}/.venv/bin/python" \
        -m genesis.cc.session_cap --existing "$n" 2>/dev/null || true)
    _cap_action=$(printf '%s\n' "$out" | sed -n '1p')
    _cap_msg=$(printf '%s\n' "$out" | sed -n '2p')
    _cap_reason=$(printf '%s\n' "$out" | sed -n '3p')
}

# True when the box can take another claude. Explains itself on a refusal.
# Called TWICE by design — once before the prompt so RECLAIM can be disclosed,
# and again in the final gate so the verdict acted on is the freshest taken.
_capacity_permits() {
    _capacity_verdict
    case "$_cap_action" in
        ALLOW) return 0 ;;
        RECLAIM)
            # RECLAIM covers a full box (tradeable) and the OOM floor (not).
            # Below the floor, starting a process risks an OOM that takes every
            # session with it, so it is refused rather than offered — the same
            # place the create path refuses when there is nothing to trade.
            if [ "$_cap_reason" = "oom_floor" ]; then
                echo "cc-slot: ${SESSION_NAME} has no claude running, but ${_cap_msg}" >&2
                echo "cc-slot: attaching to the slot's shell instead — free memory and reconnect to rebuild." >&2
                return 1
            fi
            return 0
            ;;
        DENY)
            echo "cc-slot: ${SESSION_NAME} has no claude running, but the capacity gate declined to start one: ${_cap_msg}" >&2
            echo "cc-slot: attaching to the slot's shell instead — free a slot and reconnect to rebuild." >&2
            return 1
            ;;
        *)
            # No complete verdict — fail CLOSED. "Fail open like create" was an
            # earlier justification and it was false: create falls through to
            # `_cap_fail_open`, which still enforces a cap and the OOM floor.
            echo "cc-slot: ${SESSION_NAME} has no claude running, but the capacity gate returned no usable verdict — not starting one without it." >&2
            echo "cc-slot: attaching to the slot's shell instead." >&2
            return 1
            ;;
    esac
}

_revalidate_heal() {
    _panes_now=$(tmux list-panes -s -t "=$SESSION_NAME" \
        -F '#{pane_id} #{pane_pid} #{pane_current_command}' 2>/dev/null || true)
    _fresh_row=$(printf '%s\n' "$_panes_now" | head -1)
    _fresh_pane=${_fresh_row%% *}
    # Full remainder after id+pid, NOT field 3: a process name containing a
    # space must not be truncated in the sentence the operator reads.
    _fresh_cmd=${_fresh_row#* * }
    _pane_pids_now=$(printf '%s\n' "$_panes_now" | cut -d' ' -f2 | tr '\n' ' ')
    if [ -z "$_panes_now" ] || [ "$_fresh_pane" != "$_HEAL_PANE" ]; then
        echo "cc-slot: ${SESSION_NAME}'s pane changed while preparing — attaching instead." >&2
        _HEAL=0
        return 0
    fi
    if [ "$(_probe_liveness $_pane_pids_now)" != "POISONED" ]; then
        echo "cc-slot: ${SESSION_NAME} came alive while preparing — attaching instead." >&2
        _HEAL=0
    fi
}

if [ "$_HEAL" = "1" ]; then
    _revalidate_heal
fi

if [ "$_HEAL" = "1" ]; then
    # Dependency 5, asked here so a RECLAIM can be DISCLOSED in the prompt. The
    # verdict acted on is the one taken again in the final gate below; this one
    # only shapes what the operator is told.
    if _capacity_permits; then
        if [ "$_cap_action" = "RECLAIM" ]; then
            _cap_warn="   NOTE: ${_cap_msg}"
        fi
    else
        _HEAL=0
    fi
fi

if [ "$_HEAL" = "1" ]; then
    # THE DESTRUCTIVE DECISION BELONGS TO THE OPERATOR, and this is the only
    # gate on it. `respawn-pane -k` kills the pane's process GROUP, so whatever
    # lives in that pane is gone — precisely the question no probe can answer
    # from outside it (see the note at the detection site above). The person at
    # this prompt can look at the pane. They are asked, and the default is no.
    #
    # Shape, tty handling and default-deny mirror `_cap_reclaim`'s ATTACHED
    # confirm earlier in this script: the same "destructive, ask first" case,
    # already the settled pattern here rather than a new one invented for this.
    #
    # ONE guard, and it is the question that matters: can this process reach a
    # controlling terminal. A `-t 0` test was here too and was WRONG in both
    # directions — it refused a caller whose stdin was merely redirected while
    # a human sat at the tty (the very case the /dev/tty read exists for), and
    # it made that read unreachable in exactly that case. Opening /dev/tty
    # fails without a controlling terminal, which is what a daemon or a
    # detached background process actually is.
    #
    # Read from /dev/tty, never stdin: a caller may redirect stdin, and the
    # prompt must not silently consume something else's input as an answer.
    # The redirection is probed inside a SUBSHELL whose stderr is discarded.
    # `: >/dev/tty 2>/dev/null` does NOT work: the redirection is performed by
    # the shell BEFORE the command runs, so its failure message goes to the
    # shell's stderr, not the command's — MEASURED end-to-end, every
    # terminal-less entrance printed
    # "cc-slot.sh: line N: /dev/tty: No such device or address"
    # above the door's own explanation. Only a real run surfaced it; the test
    # fakes never had a missing /dev/tty to fail on.
    if ! ( : >/dev/tty ) 2>/dev/null; then
        # No controlling terminal, so nobody can be asked: report what was
        # seen and the command that fixes it, then attach.
        # WHICH entrances land here, precisely — an earlier version of this
        # comment said "background or dashboard" and the dashboard half was
        # WRONG. MEASURED: the dashboard terminal spawns its shell under a pty
        # (dashboard/terminal_session.py), so it HAS a controlling terminal and
        # takes the prompt branch, not this one. What lands here is a genuinely
        # detached process — a background CC session, whose subprocesses have
        # no ctty at all. That distinction is load-bearing: the dashboard is a
        # NON-operator origin, so it is the one entrance where the capacity
        # gate above can return DENY, and it still reaches the respawn.
        echo "cc-slot: ${SESSION_NAME} has no claude running (pane is running '${_fresh_cmd}')." >&2
        echo "cc-slot: no terminal here to confirm a rebuild — attaching as-is." >&2
        echo "cc-slot: to rebuild it: tmux kill-session -t ${SESSION_NAME}, then reconnect." >&2
        _HEAL=0
    else
        echo "" >&2
        echo "!  ${SESSION_NAME} has no Claude Code running." >&2
        echo "   Its pane is currently running '${_fresh_cmd}'." >&2
        echo "   Rebuilding restarts that pane and ends whatever is in it." >&2
        if [ -n "$_cap_warn" ]; then
            echo "$_cap_warn" >&2
        fi
        # BOUNDED: an abandoned prompt must not hold the door open forever, so
        # the read times out and the timeout lands on "" — the default-deny
        # below then handles it, giving one refusal path rather than several.
        #
        # SIGINT is deliberately NOT trapped. Ctrl-C aborts the door, exactly
        # as cancelling `_cap_reclaim`'s prompt does (`exit 1`), so the two
        # confirms in this script behave the same way. Trapping it was tried
        # and is WORSE: bash restarts an interrupted `read`, so the trap fired,
        # the prompt stayed up, and the door hung for the rest of the timeout —
        # MEASURED, the operator lost the terminal with no message at all. An
        # abort at least ends promptly and never rebuilds anything.
        # What the operator is consenting to, captured so the action can be
        # checked against it below.
        _disclosed_cmd="$_fresh_cmd"
        _heal_confirm=""
        IFS= read -r -t 120 -p "   Rebuild this slot? [y/N] > " _heal_confirm </dev/tty \
            || _heal_confirm=""
        case "$_heal_confirm" in
            y | Y | yes | YES) ;;
            *)
                echo "cc-slot: left as-is — attaching to the slot without rebuilding." >&2
                _HEAL=0
                ;;
        esac
    fi
fi

if [ "$_HEAL" = "1" ]; then
    # The answer is in. NOW take the lock and re-establish from scratch the two
    # facts the respawn depends on — the pane is still the one that was probed,
    # and still has no claude in it. Everything above was read before a human
    # was asked, and a human takes unbounded time.
    _acquire_heal_lock
    _revalidate_heal
fi

if [ "$_HEAL" = "1" ] && ! _capacity_permits; then
    # Dependency 5, RE-DERIVED. The earlier verdict is up to 120s old by now
    # and its session count older still; another slot can have started or free
    # memory fallen inside that window, and a stale ALLOW would put the
    # measured ~3GB process below the current floor on a swapless box.
    _HEAL=0
fi

if [ "$_HEAL" = "1" ] && [ "${_disclosed_cmd+set}" = "set" ] \
   && [ "$_fresh_cmd" != "$_disclosed_cmd" ]; then
    # CONSENT IS FOR A STATE, NOT A SLOT. The operator said yes to destroying
    # a pane running `$_disclosed_cmd`; the re-validation above just re-read
    # that pane and it is running something else now. Re-checking only pane
    # IDENTITY and absence-of-claude would let a yes given for an idle `bash`
    # destroy the editor started during the same window the prompt's own
    # timeout allows for.
    echo "cc-slot: ${SESSION_NAME}'s pane is now running '${_fresh_cmd}', not '${_disclosed_cmd}' as shown — not rebuilding on an answer given for something else." >&2
    _HEAL=0
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
    # The action is ONE tmux command, and that is the point of this design.
    #
    # It replaced a keystroke ladder (C-c, C-u, type the line, Enter) that had
    # to RE-CREATE by hand everything `new-session` provides. That obligation is
    # open-ended, and review found four separate members of it across three
    # rounds — the OAuth token, the environment, TMPDIR, the capacity gate —
    # each fixed one at a time while the next waited. `respawn-pane` runs the
    # command from the tmux SERVER's environment, exactly as `new-session`
    # does, so create and heal share one execution context instead of one
    # hand-maintained imitation of it. What remains inheritable (PATH) is
    # pinned in `_PANE_ENV` above.
    #
    # `-k` kills the pane's existing process GROUP. Nothing above infers
    # whether that is safe any more — the operator was shown what the pane
    # is running and said yes. What DOES still guard this point is
    # non-inferential: the pane is the one that was probed, and no claude
    # has appeared in it since.
    #
    # Session env must be set BEFORE the respawn — the respawned command reads
    # it at exec. A failed set stands the heal down rather than respawning with
    # stale values, which is the exact defect this design exists to remove.
    _heal_ok=1
    for _kv in "${_PANE_ENV[@]}"; do
        # `=` targets the session EXACTLY (tmux prefix-matches otherwise, the
        # same trap `has-session` has), and `--` stops a value that begins with
        # a dash being read as an option.
        tmux set-environment -t "=$SESSION_NAME" -- "${_kv%%=*}" "${_kv#*=}" \
            2>/dev/null || _heal_ok=0
    done
    if [ "$_heal_ok" != "1" ]; then
        echo "cc-slot: could not set the slot's environment — not relaunching with stale values; attaching instead." >&2
    else
        # The exit status IS the delivery check: no separate "did it land"
        # question, unlike a keystroke that tmux accepts and a shell ignores.
        if tmux respawn-pane -k -t "$_HEAL_PANE" "$_PANE_CMD" 2>/dev/null; then
            :
        else
            echo "cc-slot: could not relaunch in the pane — attaching without a relaunch." >&2
        fi
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

# Dependency 4, and it guards the ATTACH path too, not just a rebuild.
# `-A` CREATES when the session is gone, and a create reached this way has
# traversed none of the create gates — not capacity, not the OAuth gate.
# `_SESSION_EXISTS` was latched near the top of this run, and a great deal
# happens after it: two liveness probes, the OAuth gate's 30s, the lock's 45s,
# and a 120s human prompt. The latch and the `-A` both PRE-DATE the rebuild, so
# this is not a defect the rebuild introduced — but the prompt widens the
# window enormously, which is what makes it worth closing here.
# Exiting is the honest repair: reconnecting re-enters the create path with
# every gate intact, where creating from here would silently skip them.
if [ "$_SESSION_EXISTS" = "1" ] \
   && ! tmux has-session -t "=$SESSION_NAME" 2>/dev/null; then
    echo "cc-slot: ${SESSION_NAME} disappeared while this door was preparing." >&2
    echo "cc-slot: not creating it from here — a session created on this path would" >&2
    echo "cc-slot: skip the capacity and login gates. Reconnect to create it cleanly." >&2
    exit 1
fi

# Exactly ONE new-session on every path (create, alive-attach, heal-attach).
# On an attach `-A` discards the command argument harmlessly; the heal above is
# purely a prelude to it.
exec tmux -u new-session -A -s "$SESSION_NAME" \
    "${_PANE_ENV_FLAGS[@]}" \
    "$_PANE_CMD"
