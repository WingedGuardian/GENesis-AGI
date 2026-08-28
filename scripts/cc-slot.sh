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
            echo "  ${name}  ${state}  (last activity: ${activity})" >&2
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
if tmux has-session -t "=$SESSION_NAME" 2>/dev/null; then
    _SESSION_EXISTS=1  # bypass cap check; also skips the OAuth gate below —
                       # attach does NOT re-run the pane command, so any token
                       # injection would be moot (and would waste a probe).
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
if [ "$_SESSION_EXISTS" = "0" ] && [ "$_slot_oauth_mode" != "off" ] && [ "$_HAS_BARE" = "0" ]; then
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
exec tmux -u new-session -A -s "$SESSION_NAME" \
    -e "GENESIS_SLOT=${SLOT}" \
    -e "GENESIS_CC_PERMISSION_MODE=${GENESIS_CC_PERMISSION_MODE:-auto}" \
    -e "CLAUDE_CODE_TMPDIR=$CLAUDE_CODE_TMPDIR" \
    -e "LANG=$LANG" \
    "${_OAUTH_SRC}cd ${GENESIS_ROOT} && claude ${CC_PERM_FLAG}${CLAUDE_ARGS_Q}; __ec=\$?; ${GENESIS_ROOT}/scripts/cc_exit_capture.sh ${SLOT} \$__ec >/dev/null 2>&1; exit \$__ec"
