# shellcheck shell=bash
# guardian_pause.sh — stand the host Guardian down across a genesis-server restart.
#
# Sourced by scripts/update.sh; written so another deploy path that restarts
# genesis-server can source it too. Pausing the host Guardian's gateway before a
# restart keeps it from reading the restart as an outage (a false CRITICAL
# "Genesis down" alert and a paid Claude Code diagnosis).
#
# CALLER CONTRACT:
#   - set VENV_DIR (the pause reads guardian_remote.yaml with the venv's python);
#   - arm the EXIT trap that calls _guardian_resume, ideally BEFORE calling
#     _guardian_pause. Resume is flag-guarded, so an early trap is a no-op until a
#     pause is accepted. The lib does not arm traps itself: a trap is one global
#     slot per signal, and only the caller knows what else it must compose with.
#   - a caller holding the deploy lock on a fd keeps its number in
#     _UPDATE_LOCK_FD (that exact name); the renewer is launched with that fd
#     CLOSED, because a background process that inherits it keeps the lock after
#     the caller exits. A lock fd held under any other name is not closed.
#
# GUARDIAN_PAUSE_TTL and GUARDIAN_PAUSE_RENEW_MAX are plain assignments on purpose:
# a caller derives its health window from them (update.sh's HEALTH_GUARDIAN_COVER),
# so an inherited environment value (a TTL of 0 gives a window of 0) must not be
# able to reach them.

# Guardian pause across the deploy's server restart (PR-2). Coords are resolved
# lazily inside _guardian_pause and stashed here for resume (kept SEPARATE from
# the HOST_IP/SSH_KEY globals update.sh's _sync_deploy_targets owns).
_GUARDIAN_PAUSED=""
_GUARDIAN_HOST=""
_GUARDIAN_KEY=""
_GUARDIAN_RENEW_PID=""   # PID of the background lease-renewer (P2 #4), if running
_GUARDIAN_PARENT_PID=""  # the deploy process the renewer serves
_GUARDIAN_PARENT_START="" # its start time (/proc stat field 22); with the pid, its identity

# "<state> <starttime>" for a pid, from /proc/<pid>/stat; empty when /proc cannot
# answer (no such process, or no /proc). Fields are counted AFTER the last ")",
# because field 2 is the command name in parentheses and may itself hold spaces.
_guardian_proc_start() {
    local stat
    stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 0
    stat="${stat##*) }"
    # shellcheck disable=SC2086 # word-splitting the numeric fields is the point
    set -- $stat
    # Now $1 is field 3 (state) and ${20} is field 22 (starttime).
    [ -n "${20:-}" ] && printf '%s %s\n' "$1" "${20}"
}

# Is the deploy that started the renewer still THAT running process? `kill -0`
# alone says yes for a zombie (a SIGKILLed deploy its parent has not reaped) and
# for an unrelated process that reused the pid — either way the renewer would keep
# re-pausing the Guardian over a deploy that is gone. So: same start time, and not
# a zombie. Where /proc could not answer at pause time, `kill -0` is all there is.
_guardian_parent_alive() {
    local now
    if [ -z "$_GUARDIAN_PARENT_START" ]; then
        kill -0 "$_GUARDIAN_PARENT_PID" 2>/dev/null
        return
    fi
    now="$(_guardian_proc_start "$_GUARDIAN_PARENT_PID")"
    [ -n "$now" ] || return 1
    [ "${now%% *}" != Z ] || return 1
    [ "${now#* }" = "$_GUARDIAN_PARENT_START" ]
}
# Generous TTL: the EXIT-trap resume ends the pause early on success, so this only
# matters if the deploy is SIGKILLed (the host's expires_at then self-heals after
# this long). The bound is the server-DOWN window (~3-15 min), not the total pause
# (the long host-sync runs server-up); capped at the guardian's 3600.
GUARDIAN_PAUSE_TTL=1800
# Bounded lease-renew (P2 #4): the stop→restart window has NO upper bound (the
# bootstrap does unbounded network work — installer downloads, npm), so a fixed
# TTL can expire mid-deploy and re-fire the very alerts the pause suppresses. A
# background renewer re-issues `pause` every TTL/2 while the server is down. CAPPED
# so an orphaned renewer self-terminates in ~ RENEW_MAX * TTL/2 (here ~1h) instead
# of pausing the guardian forever — and since this lib, it also stops at its next
# wake (at most TTL/2 later), before sending another pause, once the deploy that
# started it is gone (see _guardian_renew_loop).
GUARDIAN_PAUSE_RENEW_MAX=4

# ── Guardian pause / resume across the server restart (PR-2) ─────────────────
# BEST-EFFORT ONLY: at update.sh's pause site `set -e` is live and the ERR trap is
# not yet armed, so a bare SSH failure would ABORT the deploy — every SSH is
# `timeout … || true` (version-verb style, NOT fetch style, which exit 1s). Host
# coords are resolved lazily here (no hoisted resolver → nothing enters the
# phase-order chain) into separate globals. The `pause` verb only stands the
# Guardian down once PR-1's gateway is on the host; against an old gateway it
# errors and we proceed unpaused (safe, dark). No-op when no host is configured.
_guardian_pause() {
    local cfg="$HOME/.genesis/guardian_remote.yaml"
    [ -f "$cfg" ] || return 0
    local hip hus key
    hip=$("$VENV_DIR/bin/python" -c "import yaml,pathlib;print(yaml.safe_load(pathlib.Path('$cfg').read_text()).get('host_ip',''))" 2>/dev/null || true)
    hus=$("$VENV_DIR/bin/python" -c "import yaml,pathlib;print(yaml.safe_load(pathlib.Path('$cfg').read_text()).get('host_user','ubuntu'))" 2>/dev/null || echo ubuntu)
    # Honor the configured ssh_key (guardian_remote.yaml), expanding a leading ~,
    # and fall back to the historical default when the field is absent/empty.
    key=$("$VENV_DIR/bin/python" -c "import yaml,pathlib,os;k=yaml.safe_load(pathlib.Path('$cfg').read_text()).get('ssh_key','') or '';print(os.path.expanduser(k))" 2>/dev/null || true)
    [ -n "$key" ] || key="$HOME/.ssh/genesis_guardian_ed25519"
    [ -n "$hip" ] && [ -f "$key" ] || return 0
    _GUARDIAN_HOST="${hus:-ubuntu}@${hip}"
    _GUARDIAN_KEY="$key"
    # Don't clobber a pause we did not create (P2 #1): if the gateway already has an
    # UNEXPIRED pause (an operator or another workflow set it), leave it intact —
    # proceed WITHOUT pausing and WITHOUT arming resume, so our EXIT never removes
    # their pause (a pre-existing pause already covers our restart window). Against
    # an OLD gateway with no `paused` verb the query errors/returns non-JSON → no
    # match → we fall through and pause as before (backward-compatible). The pipe is
    # in an `if` condition, so a failing ssh can't abort the deploy.
    if timeout 15 ssh -i "$_GUARDIAN_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
        "$_GUARDIAN_HOST" paused 2>/dev/null | grep -q '"paused": true'; then
        echo "  Guardian already paused (operator/other) — leaving it intact; not arming our resume"
        return 0
    fi
    # Only mark paused (and so arm the resume) if the gateway ACCEPTED the verb.
    # Against an OLD gateway (no `pause <ttl>` grammar — needs PR-1 deployed) or an
    # unreachable host this fails; we then proceed unpaused with a VISIBLE warning
    # instead of a misleading "paused" + silence. The `if` is set -e-safe (a failing
    # condition never aborts), so a denied/unreachable pause can't abort the deploy.
    if timeout 15 ssh -i "$_GUARDIAN_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
        "$_GUARDIAN_HOST" "pause $GUARDIAN_PAUSE_TTL" >/dev/null 2>&1; then
        _GUARDIAN_PAUSED=1
        echo "  Guardian paused across the restart (ttl ${GUARDIAN_PAUSE_TTL}s)"
        # Start the bounded lease renewer so an over-TTL downtime can't expire the
        # pause mid-deploy (P2 #4). _guardian_resume (and so the caller's EXIT
        # trap) kills it. Redirect its fds so it (and its `sleep` child) don't hold
        # the deploy's stdout/stderr — otherwise a lingering sleep would keep the
        # pipe open. And CLOSE the deploy lock fd for it: the renewer's `sleep`
        # outlives the kill in _guardian_resume (only the bash is killed), and an
        # inherited lock fd kept the lock held for up to TTL/2 after EVERY run,
        # clean exit included — up to about an hour after a kill.
        _GUARDIAN_PARENT_PID="$$"
        _GUARDIAN_PARENT_START="$(_guardian_proc_start "$$" || true)"
        _GUARDIAN_PARENT_START="${_GUARDIAN_PARENT_START#* }"
        if [ -n "${_UPDATE_LOCK_FD:-}" ]; then
            _guardian_renew_loop {_UPDATE_LOCK_FD}>&- >/dev/null 2>&1 &
        else
            _guardian_renew_loop >/dev/null 2>&1 &
        fi
        _GUARDIAN_RENEW_PID=$!
    else
        echo "  WARNING: guardian pause not accepted (old gateway or host unreachable) — proceeding unpaused" >&2
    fi
}

_guardian_resume() {
    [ "${_GUARDIAN_PAUSED:-}" = 1 ] || return 0
    # Stop the lease renewer FIRST so it cannot re-pause after we resume (P2 #4).
    # We never `wait` the renewer before this point, so its PID stays held (running,
    # or a zombie once the bounded loop self-exits) and CANNOT be reused — so kill -0
    # reliably identifies our own process (no PID-reuse hazard, no fragile identity
    # check). `wait` reaps the renewer bash, stopping further renewals. RESIDUAL: a
    # `pause` ssh already in flight when the kill lands (~15s window, only if the
    # kill hits mid-renew) is orphaned and may complete AFTER the resume below,
    # re-asserting the pause — bounded + self-healing via the host-side TTL (≤ the
    # pause TTL, ≤30min). All steps non-aborting under set -e.
    if [ -n "${_GUARDIAN_RENEW_PID:-}" ]; then
        if kill -0 "$_GUARDIAN_RENEW_PID" 2>/dev/null; then
            kill "$_GUARDIAN_RENEW_PID" 2>/dev/null || true
        fi
        wait "$_GUARDIAN_RENEW_PID" 2>/dev/null || true
        _GUARDIAN_RENEW_PID=""
    fi
    # Clear the flag ONLY after the gateway accepts `resume`. On a transient SSH
    # failure the flag stays set so the EXIT-trap retries; if every retry fails the
    # host-side TTL (expires_at) self-heals. Clearing first would no-op the retry.
    if timeout 15 ssh -i "$_GUARDIAN_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
        "$_GUARDIAN_HOST" resume >/dev/null 2>&1; then
        _GUARDIAN_PAUSED=""
    fi
    return 0
}

_guardian_renew_loop() {
    # Re-issue `pause $TTL` every TTL/2 while the server is down, BOUNDED to
    # GUARDIAN_PAUSE_RENEW_MAX iterations. Runs in the background (started by
    # _guardian_pause); _guardian_resume — and so the caller's EXIT trap — kills it.
    # A failed renew is swallowed (best-effort, like the pause itself).
    #
    # It also stops once the deploy that started it is gone. A SIGKILLed deploy
    # runs no EXIT trap, so nothing kills this loop, and without the check it
    # would keep re-pausing the Guardian for about an hour while later deploys
    # proceed and the server may be down for real. Checked after each sleep and
    # BEFORE the renew, so no pause is sent once the parent has died.
    local i=0
    while [ "$i" -lt "$GUARDIAN_PAUSE_RENEW_MAX" ]; do
        sleep "$((GUARDIAN_PAUSE_TTL / 2))"
        _guardian_parent_alive || break
        timeout 15 ssh -i "$_GUARDIAN_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
            "$_GUARDIAN_HOST" "pause $GUARDIAN_PAUSE_TTL" >/dev/null 2>&1 || true
        i=$((i + 1))
    done
}
