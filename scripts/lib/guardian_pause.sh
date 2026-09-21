# shellcheck shell=bash
# guardian_pause.sh — pause the host Guardian's remediation across a deploy's
# server restart, with a hard host-side TTL and a bounded lease renewer.
#
# EXTRACTED VERBATIM from scripts/update.sh (2026-09-06) so the code-only
# deploy path (scripts/deploy_code_only.sh) can share it instead of growing a
# drifting replica — the function bodies are update.sh's, with exactly two
# parameterizations:
#   * GUARDIAN_PAUSE_TTL / GUARDIAN_PAUSE_RENEW_MAX become `:=` defaults a
#     caller may override BEFORE calling _guardian_pause (update.sh keeps
#     1800/4; the code-only wrapper uses a short window).
#   * The EXIT trap moved to the CALLER: update.sh's trap composes resume with
#     its temp-copy self-delete, the wrapper's composes resume with lock/state
#     cleanup — a lib cannot know either. Callers arm their trap after calling
#     _guardian_pause; _guardian_resume is a no-op unless _GUARDIAN_PAUSED=1,
#     so an unconditional trap is safe.
#
# Requires: VENV_DIR (a venv with PyYAML) set by the caller. No-op when
# ~/.genesis/guardian_remote.yaml is absent (no host configured) — presence of
# optional infrastructure is never assumed.
#
# Why a pause at all: a deploy cannot bring the container down directly, but a
# health-checker misreading a mid-deploy state can walk its remediation ladder
# toward a container restart. The gateway pause verb stands remediation down
# for the window; the host-side expires_at is the hard TTL that guarantees a
# crashed deploy can never leave the guardian silenced (its max-ahead cap is
# 3600s). The guardian still heartbeats `standdown=gateway_pause`, so the
# container watchdog does not counter-restart it for being quiet.

_GUARDIAN_PAUSED="${_GUARDIAN_PAUSED:-}"
_GUARDIAN_HOST="${_GUARDIAN_HOST:-}"
_GUARDIAN_KEY="${_GUARDIAN_KEY:-}"
_GUARDIAN_RENEW_PID="${_GUARDIAN_RENEW_PID:-}"
_GUARDIAN_TOKEN="${_GUARDIAN_TOKEN:-}"
# Generous default TTL: the caller's EXIT-trap resume ends the pause early on
# success, so this only matters if the deploy is SIGKILLed (the host's
# expires_at then self-heals after this long). Capped at the guardian's 3600.
: "${GUARDIAN_PAUSE_TTL:=1800}"
# Bounded lease-renew: a fixed TTL can expire mid-deploy, so a background
# renewer re-issues `pause` every TTL/2 — CAPPED so an orphaned renewer
# (parent died before cleanup) self-terminates in ~ RENEW_MAX * TTL/2 instead
# of pausing the guardian forever.
: "${GUARDIAN_PAUSE_RENEW_MAX:=4}"

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
    # Don't clobber a pause we did not create — atomic acquire: `pause-if-absent`
    # runs the live-pause check and the write under the gateway's pause lock,
    # so an operator pause can never be clobbered by the gap between a
    # separate `paused` query and our write
    # (Devin #1804). Responses:
    #   rc 0               -> we hold the pause; renewer/resume arm below.
    #   "already paused"   -> a live pause that is not ours; leave it intact and
    #                         do NOT arm resume (it already covers our window).
    #   "denied" / other   -> old gateway without the verb or a transport
    #                         failure: fall through to the legacy probe+pause,
    #                         which is non-atomic but only reachable there.
    _GUARDIAN_TOKEN="deploy-$$-$(date -u +%s)"
    _gw_rc=0
    # 2>&1: the gateway's "already paused"/"denied" replies are emitted on
    # stderr — merge them into _gw_out so the response dispatch below sees them.
    _gw_out="$(timeout 15 ssh -i "$_GUARDIAN_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
        "$_GUARDIAN_HOST" "pause-if-absent $GUARDIAN_PAUSE_TTL $_GUARDIAN_TOKEN" 2>&1)" || _gw_rc=$?
    if [ "$_gw_rc" -eq 0 ]; then
        :
    elif printf '%s' "$_gw_out" | grep -q 'already paused'; then
        echo "  Guardian already paused (operator/other) — leaving it intact; not arming our resume"
        return 0
    elif printf '%s' "$_gw_out" | grep -q '"denied"'; then
        # Gateway predates pause-if-absent — legacy check-then-pause. The race
        # this tolerates (an operator pause landing between `paused` and
        # `pause` gets overwritten) exists only against an old gateway.
        if timeout 15 ssh -i "$_GUARDIAN_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
            "$_GUARDIAN_HOST" paused 2>/dev/null | grep -q '"paused": true'; then
            echo "  Guardian already paused (operator/other) — leaving it intact; not arming our resume"
            return 0
        fi
        # A gateway that predates the owner grammar rejects `pause <ttl>
        # <token>` outright (its ttl check sees the token) — fall back to the
        # bare form there and clear the token so the resume and renewer take
        # the old unconditional path.
        if timeout 15 ssh -i "$_GUARDIAN_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
            "$_GUARDIAN_HOST" "pause $GUARDIAN_PAUSE_TTL $_GUARDIAN_TOKEN" >/dev/null 2>&1; then
            :
        elif timeout 15 ssh -i "$_GUARDIAN_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
            "$_GUARDIAN_HOST" "pause $GUARDIAN_PAUSE_TTL" >/dev/null 2>&1; then
            _GUARDIAN_TOKEN=""
        else
            echo "  WARNING: guardian pause not accepted (old gateway or host unreachable) — proceeding unpaused" >&2
            return 0
        fi
    else
        echo "  WARNING: guardian pause not accepted (old gateway or host unreachable) — proceeding unpaused" >&2
        return 0
    fi
    _GUARDIAN_PAUSED=1
    echo "  Guardian paused across the restart (ttl ${GUARDIAN_PAUSE_TTL}s)"
    # Start the bounded lease renewer so an over-TTL downtime can't expire
    # the pause mid-deploy. _guardian_resume (and the caller's EXIT trap)
    # kills it. Redirect its fds so it (and its `sleep` child) don't hold
    # the caller's stdout/stderr open — and close the STATION-LOCK fd, so an
    # orphaned renewer cannot extend the EXCLUSIVE hold past a SIGKILLed
    # deploy. Two callers, two variable names: the lib's own acquirers keep
    # the fd in _DEPLOY_LOCK_FD, while update.sh takes the same lock file
    # with its own inline flock in _UPDATE_LOCK_FD (scripts/update.sh) — a
    # SIGKILLed update.sh otherwise left the renewer holding the station
    # for up to RENEW_MAX × TTL/2 = 60 min, timing out the code-only deploy
    # path and every validation behind it (Codex P2, #1804). The residue was
    # bounded rather than unbounded, but that lands exactly on a retry's
    # default 600s wait, so the retry could time out against a deploy that
    # was already dead. `${VAR:-...}` keeps this lib usable standalone:
    # guardian_pause.sh does not require deploy_lock.sh OR update.sh's lock
    # to be in play, and closing a never-opened fd would be an error under
    # set -e. (Kimi P3, 2026-09-06.)
    if [ -n "${_DEPLOY_LOCK_FD:-}" ] && [ -n "${_UPDATE_LOCK_FD:-}" ]; then
        _guardian_renew_loop >/dev/null 2>&1 {_DEPLOY_LOCK_FD}>&- {_UPDATE_LOCK_FD}>&- &
    elif [ -n "${_UPDATE_LOCK_FD:-}" ]; then
        _guardian_renew_loop >/dev/null 2>&1 {_UPDATE_LOCK_FD}>&- &
    elif [ -n "${_DEPLOY_LOCK_FD:-}" ]; then
        _guardian_renew_loop >/dev/null 2>&1 {_DEPLOY_LOCK_FD}>&- &
    else
        _guardian_renew_loop >/dev/null 2>&1 &
    fi
    _GUARDIAN_RENEW_PID=$!
}

_guardian_resume() {
    [ "${_GUARDIAN_PAUSED:-}" = 1 ] || return 0
    # Stop the lease renewer FIRST so it cannot re-pause after we resume.
    # We never `wait` the renewer before this point, so its PID stays held
    # (running, or a zombie once the bounded loop self-exits) and CANNOT be
    # reused — kill -0 reliably identifies our own process. `wait` reaps the
    # renewer bash, stopping further renewals. RESIDUAL: a `pause` ssh already
    # in flight when the kill lands (~15s window) may complete AFTER the resume
    # below, re-asserting the pause — bounded + self-healing via the host-side
    # TTL. All steps non-aborting under set -e.
    if [ -n "${_GUARDIAN_RENEW_PID:-}" ]; then
        if kill -0 "$_GUARDIAN_RENEW_PID" 2>/dev/null; then
            kill "$_GUARDIAN_RENEW_PID" 2>/dev/null || true
        fi
        wait "$_GUARDIAN_RENEW_PID" 2>/dev/null || true
        _GUARDIAN_RENEW_PID=""
    fi
    # Clear the flag ONLY after the gateway accepts `resume`. On a transient SSH
    # failure the flag stays set so the caller's EXIT-trap retries; if every
    # retry fails the host-side TTL (expires_at) self-heals. Clearing first
    # would no-op the retry. With an owner token the gateway refuses to remove
    # a pause another holder wrote — that refusal is terminal (no retry will
    # ever make their pause ours), so the flag clears and their pause stands
    # (Codex P2, #1804).
    local _rout
    _rout=$(timeout 15 ssh -i "$_GUARDIAN_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
        "$_GUARDIAN_HOST" "resume${_GUARDIAN_TOKEN:+ $_GUARDIAN_TOKEN}" 2>/dev/null || true)
    if printf '%s' "$_rout" | grep -q '"ok": true'; then
        _GUARDIAN_PAUSED=""
    elif printf '%s' "$_rout" | grep -q 'owned by another holder'; then
        echo "  NOTE: gateway pause is owned by another holder — leaving it intact" >&2
        _GUARDIAN_PAUSED=""
    fi
    return 0
}

_guardian_renew_loop() {
    # Re-issue `pause $TTL` every TTL/2 while the caller works, BOUNDED to
    # GUARDIAN_PAUSE_RENEW_MAX iterations. Runs in the background (started by
    # _guardian_pause); _guardian_resume — and thus the caller's EXIT trap —
    # kills it. A failed renew is swallowed (best-effort, like the pause).
    local i=0 _pstate _powner
    while [ "$i" -lt "$GUARDIAN_PAUSE_RENEW_MAX" ]; do
        sleep "$((GUARDIAN_PAUSE_TTL / 2))"
        if [ -n "${_GUARDIAN_TOKEN:-}" ]; then
            # Atomic renew: `pause-if-owner` writes ONLY if the pause is absent
            # or already ours — a pause another holder wrote mid-deploy is never
            # overwritten by a renewal, and "owned by another holder" stops this
            # loop instead of racing them (Devin #1804). A gateway that predates
            # the verb replies "denied" → the legacy probe+pause below (its
            # probe-then-write race is reachable only there).
            _pstate=$(timeout 15 ssh -i "$_GUARDIAN_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
                "$_GUARDIAN_HOST" "pause-if-owner $GUARDIAN_PAUSE_TTL $_GUARDIAN_TOKEN" 2>&1 || true)
            if printf '%s' "$_pstate" | grep -q '"ok": true'; then
                i=$((i + 1)); continue
            fi
            if printf '%s' "$_pstate" | grep -q 'owned by another holder'; then
                break
            fi
            if ! printf '%s' "$_pstate" | grep -q '"denied"'; then
                # Transport failure / unexpected reply — skip this round rather
                # than risk a blind overwrite; the next iteration retries.
                i=$((i + 1)); continue
            fi
            # Legacy gateway: ownership check before extending — a pause file
            # overwritten by another holder is not ours to refresh (Codex P2).
            _pstate=$(timeout 15 ssh -i "$_GUARDIAN_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
                "$_GUARDIAN_HOST" paused 2>/dev/null || true)
            [ -n "$_pstate" ] || { i=$((i + 1)); continue; }
            _powner=$(printf '%s' "$_pstate" | sed -n 's/.*"owner": "\([^"]*\)".*/\1/p')
            if printf '%s' "$_pstate" | grep -q '"paused": true' && \
               [ "$_powner" != "$_GUARDIAN_TOKEN" ]; then
                break
            fi
        fi
        timeout 15 ssh -i "$_GUARDIAN_KEY" -o BatchMode=yes -o ConnectTimeout=10 \
            "$_GUARDIAN_HOST" "pause $GUARDIAN_PAUSE_TTL${_GUARDIAN_TOKEN:+ $_GUARDIAN_TOKEN}" >/dev/null 2>&1 || true
        i=$((i + 1))
    done
}
