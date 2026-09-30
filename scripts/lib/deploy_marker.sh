# shellcheck shell=bash
# deploy_marker.sh — the deploy-in-progress marker, and the tracked paths a deploy
# may find dirty without refusing.
#
# Sourced by scripts/restore.sh (the acquire/release helpers) and scripts/update.sh
# (the holder check its cleanup calls, and EPHEMERAL_DIRTY_RE). A caller that uses
# the holder check without sourcing this lib gets status 127, which reads as "dead"
# and would delete a live holder's marker, so tests pin that update.sh sources it.
# The functions print nothing: each caller logs in its own format at the call site,
# and reads DEPLOY_MARKER_HOLDER to name a refusing holder.

# ── Deploy-in-progress marker ────────────────────────────────────────
# While a deploy holds genesis-server stopped (a restart, or restore.sh's
# multi-minute DB rebuild), the autonomy watchdog (genesis-watchdog.timer, ~every
# 300s) would otherwise see the inactive unit and restart the server in the middle
# of it. The marker is the bare live PID that env.update_in_progress() honours, so
# the watchdog DEFERS until the holder's EXIT trap clears it. A live foreign holder
# (a dashboard update, update.sh's direct path, another restore) is refused, never
# clobbered, and the marker is removed only while it still holds OUR pid.
DEPLOY_MARKER_FILE="${GENESIS_HOME:-$HOME/.genesis}/update_in_progress.pid"
DEPLOY_MARKER_HOLDER=""
_DEPLOY_MARKER_HELD=false

# Is <pid> a LIVE holder of the marker? `kill -0` alone says yes for a zombie (a
# killed deploy its parent has not reaped), which would read as held until the
# parent reaps it: new deploys refused, and the watchdog kept from reviving a
# down server. So: running and not a zombie. Nothing here compares clocks, so a
# wall-clock step can never make a live holder read as stale. A reused pid still
# reads as live; recording the holder's start tick with its pid is the complete
# fix and changes the marker format for every writer (tracked separately).
# Where /proc cannot answer, `kill -0` is all there is. Mirrored for the Python
# reader by genesis.env._marker_holder_live.
_deploy_marker_holder_live() {
    local pid="$1" stat
    kill -0 "$pid" 2>/dev/null || return 1
    stat="$(cat "/proc/$pid/stat" 2>/dev/null)" || return 0
    stat="${stat##*) }"
    # The field after the parenthesised command name is the process state.
    [ "${stat%% *}" != Z ]
}

# Returns 0 once the marker holds our pid; 1 when a LIVE foreign process holds it
# (its pid is left in DEPLOY_MARKER_HOLDER); 2 when the marker cannot be written
# (DEPLOY_MARKER_HOLDER stays empty). A dead, zombie or garbage pid is a stale
# marker, so it is simply replaced. Every failure is CHECKED, never assumed: the
# callers run this as a condition, where `set -e` does not apply, so an unchecked
# write would report a marker that was never written, and the watchdog, which
# defers only on a marker it can read, could then restart the server mid-deploy.
_acquire_deploy_marker() {
    DEPLOY_MARKER_HOLDER=""
    mkdir -p "$(dirname "$DEPLOY_MARKER_FILE")" 2>/dev/null || return 2
    if [ -f "$DEPLOY_MARKER_FILE" ]; then
        local _other
        _other="$(cat "$DEPLOY_MARKER_FILE" 2>/dev/null || true)"
        if [[ "$_other" =~ ^[0-9]+$ ]] && [ "$_other" -gt 1 ] && [ "$_other" != "$$" ] \
            && _deploy_marker_holder_live "$_other"; then
            DEPLOY_MARKER_HOLDER="$_other"
            return 1
        fi
    fi
    { printf '%s\n' "$$" > "$DEPLOY_MARKER_FILE"; } 2>/dev/null || return 2
    _DEPLOY_MARKER_HELD=true
}

_release_deploy_marker() {
    $_DEPLOY_MARKER_HELD || return 0
    # Remove only if it is still OUR pid (a later deploy may have taken over).
    if [ -f "$DEPLOY_MARKER_FILE" ] && [ "$(cat "$DEPLOY_MARKER_FILE" 2>/dev/null || true)" = "$$" ]; then
        rm -f "$DEPLOY_MARKER_FILE"
    fi
    _DEPLOY_MARKER_HELD=false
}

# ── Tracked files a deploy may find dirty ────────────────────────────
# Known-ephemeral tracked files: rewritten in place, safe to ignore, so they never
# block a deploy on their own. REAL tracked changes still refuse. Each alternative
# anchors the exact porcelain path (a single space precedes it), so only these
# exact paths are excused — `src/AGENTS.md` would still refuse. Today:
#   - top-level `AGENTS.md` (GitNexus rewrites its auto-stat block);
#   - `config/procedure_triggers.yaml` (now .gitignored and regenerated at
#     bootstrap; transitional for installs that still track it);
#   - `.claude/settings.local.json`, `.serena/project.yml` and
#     `src/genesis/identity/USER.md` (install-local files that were once tracked;
#     transitional while installs carry them through the de-tracking).
# update.sh documents each entry's history where it clears them before its merge.
EPHEMERAL_DIRTY_RE=' AGENTS\.md$| config/procedure_triggers\.yaml$| \.claude/settings\.local\.json$| \.serena/project\.yml$| src/genesis/identity/USER\.md$'
