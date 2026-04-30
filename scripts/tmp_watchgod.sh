#!/usr/bin/env bash
# tmp_watchgod.sh — Dual-zone temp directory protection.
#
# Runs as a standalone systemd user service, independent of Genesis.
# Monitors two zones:
#   Zone A: CC temp (~/.genesis/cc-tmp/) — tiered budget enforcement
#   Zone B: System /tmp — gentle time-based housekeeping
#
# Reads config from ~/.genesis/config/watchgod.conf
# Writes status to ~/.genesis/watchgod_state.json
# Logs to ~/.genesis/logs/tmp_watchgod.log

set -euo pipefail

POLL_INTERVAL=30
CONF_FILE="$HOME/.genesis/config/watchgod.conf"
STATE_FILE="$HOME/.genesis/watchgod_state.json"
LOG_FILE="$HOME/.genesis/logs/tmp_watchgod.log"
ALERT_DIR="$HOME/.genesis/alerts"

# Defaults (overridden by config)
CC_TMP_DIR="$HOME/.genesis/cc-tmp"
CC_TMP_BUDGET_MB=500
SACRED_GROUND_MB=150

# ── Load config ──────────────────────────────────────────────
load_config() {
    if [[ -f "$CONF_FILE" ]]; then
        # shellcheck source=/dev/null
        source "$CONF_FILE"
    fi
}

# ── Logging ──────────────────────────────────────────────────
log() {
    local level="$1"; shift
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [$level] $*" >> "$LOG_FILE"
}

# ── Helpers ──────────────────────────────────────────────────
dir_usage_mb() {
    # Total disk usage of a directory in MB
    du -sm "$1" 2>/dev/null | awk '{print $1}' || echo 0
}

tmp_usage_pct() {
    # Percentage of /tmp filesystem used
    df --output=pcent /tmp 2>/dev/null | tail -1 | tr -d ' %' || echo 0
}

fs_free_mb() {
    # Free space on the filesystem containing the given path, in MB
    df -BM --output=avail "$1" 2>/dev/null | tail -1 | tr -d ' M' || echo 999999
}

write_state() {
    local cc_tier="$1" cc_used="$2" sys_tier="$3" sys_pct="$4"
    local is_tmpfs="false"
    if df -T /tmp 2>/dev/null | grep -q tmpfs; then
        is_tmpfs="true"
    fi
    local tmp="${STATE_FILE}.tmp"
    cat > "$tmp" <<EOF
{
  "cc_tmp": {"tier": "$cc_tier", "used_mb": $cc_used, "budget_mb": $CC_TMP_BUDGET_MB, "sacred_mb": $SACRED_GROUND_MB},
  "system_tmp": {"tier": "$sys_tier", "used_pct": $sys_pct, "is_tmpfs": $is_tmpfs},
  "poll_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
    mv "$tmp" "$STATE_FILE"
}

# ── Zone A: CC Temp ──────────────────────────────────────────
#
# Tiers (% of budget):
#   Green  : < 50%  — no action
#   Yellow : > 50%  — clean stale session dirs + old temp files
#   Orange : > 75%  — yellow + delete caches + kill idle sessions + alert
#   Red    : > 90% OR fs free < sacred — nuclear cleanup + emergency alert

clean_cc_yellow() {
    log INFO "Zone A YELLOW — cleaning stale session dirs and temp files"

    # Clean session dirs with mtime > 7 days
    find "$CC_TMP_DIR" -mindepth 2 -maxdepth 2 -type d -path "*/claude-*/???*" \
        -mtime +7 -exec rm -rf {} + 2>/dev/null || true

    # Clean old temp files (*.tmp, *.env, *.yaml) > 1 hour old
    find "$CC_TMP_DIR" -type f \( -name "*.tmp" -o -name "*.env" -o -name "*.yaml" \) \
        -mmin +60 -delete 2>/dev/null || true
}

clean_cc_orange() {
    clean_cc_yellow
    log WARN "Zone A ORANGE — deleting caches, killing idle sessions"

    # Delete claude-skills cache (~35MB, CC re-clones on demand)
    find "$CC_TMP_DIR" -type d -name "claude-skills" -exec rm -rf {} + 2>/dev/null || true

    # Delete tsx cache (~1.2MB, rebuilt automatically)
    find "$CC_TMP_DIR" -type d -name "tsx-*" -exec rm -rf {} + 2>/dev/null || true

    # Kill idle CC tmux sessions (unattached, idle > 2h)
    while IFS= read -r session; do
        [[ -z "$session" ]] && continue
        local sname
        sname=$(echo "$session" | cut -d: -f1)
        if [[ "$sname" =~ ^cc- ]]; then
            local last_activity
            last_activity=$(tmux display-message -t "$sname" -p '#{session_activity}' 2>/dev/null || echo 0)
            local now
            now=$(date +%s)
            local idle_s=$(( now - last_activity ))
            if (( idle_s > 7200 )); then
                log WARN "Killing idle CC session: $sname (idle ${idle_s}s)"
                tmux kill-session -t "$sname" 2>/dev/null || true
            fi
        fi
    done < <(tmux list-sessions -F '#{session_name}:#{session_attached}' 2>/dev/null | grep ':0$' || true)

    # Alert
    mkdir -p "$ALERT_DIR"
    touch "$ALERT_DIR/tmp_warning"
}

clean_cc_red() {
    log WARN "Zone A RED — NUCLEAR cleanup, preserving active session"

    # Find the most recently modified session UUID dir (the active workspace)
    local newest_session=""
    newest_session=$(find "$CC_TMP_DIR" -mindepth 2 -maxdepth 2 -type d -path "*/claude-*" \
        -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | awk '{print $2}') || true

    # Delete ALL session dirs EXCEPT the newest one and files modified in last 60s
    find "$CC_TMP_DIR" -mindepth 1 -maxdepth 1 -type d | while IFS= read -r dir; do
        # Skip if this contains the active session
        if [[ -n "$newest_session" && "$newest_session" == "$dir/"* ]]; then
            continue
        fi
        rm -rf "$dir" 2>/dev/null || true
    done

    # Delete all reclaimable files except those modified in last 60s
    find "$CC_TMP_DIR" -type f -not -newermt '60 seconds ago' \
        -not -path "$newest_session/*" -delete 2>/dev/null || true

    # Delete caches unconditionally
    find "$CC_TMP_DIR" -type d \( -name "claude-skills" -o -name "tsx-*" \) \
        -exec rm -rf {} + 2>/dev/null || true

    # Kill ALL idle CC sessions
    while IFS= read -r sname; do
        [[ -z "$sname" ]] && continue
        [[ "$sname" =~ ^cc- ]] && tmux kill-session -t "$sname" 2>/dev/null || true
    done < <(tmux list-sessions -F '#{session_name}:#{session_attached}' 2>/dev/null \
             | grep ':0$' | cut -d: -f1 || true)

    # Emergency alert
    mkdir -p "$ALERT_DIR"
    touch "$ALERT_DIR/tmp_emergency"
    log WARN "Zone A RED — nuclear cleanup complete"
}

check_cc_tmp() {
    mkdir -p "$CC_TMP_DIR"
    local used_mb
    used_mb=$(dir_usage_mb "$CC_TMP_DIR")
    local free_mb
    free_mb=$(fs_free_mb "$CC_TMP_DIR")

    local threshold_yellow=$(( CC_TMP_BUDGET_MB * 50 / 100 ))
    local threshold_orange=$(( CC_TMP_BUDGET_MB * 75 / 100 ))
    local threshold_red=$(( CC_TMP_BUDGET_MB * 90 / 100 ))

    local tier="green"

    if (( used_mb > threshold_red )) || (( free_mb < SACRED_GROUND_MB )); then
        tier="red"
        clean_cc_red
    elif (( used_mb > threshold_orange )); then
        tier="orange"
        clean_cc_orange
    elif (( used_mb > threshold_yellow )); then
        tier="yellow"
        clean_cc_yellow
    fi

    echo "$tier:$used_mb"
}

# ── Zone B: System /tmp ──────────────────────────────────────
#
# Tiers (% of filesystem):
#   Green  : < 50%  — no action
#   Yellow : 50-70% — clean files not accessed in 7+ days
#   Orange : 70-85% — clean files not accessed in 3+ days + alert
#   Red    : > 85%  — aggressive cleanup + emergency alert

clean_sys_yellow() {
    log INFO "Zone B YELLOW — cleaning /tmp files not accessed in 7+ days"
    find /tmp -type f -not -path "*/tmux-*" -not -path "*/pytest-*" -not -name "*.sock" \
        -atime +7 -delete 2>/dev/null || true
    find /tmp -mindepth 1 -type d -empty -not -path "*/tmux-*" -not -path "*/pytest-*" \
        -delete 2>/dev/null || true
}

clean_sys_orange() {
    clean_sys_yellow
    log WARN "Zone B ORANGE — cleaning /tmp files not accessed in 3+ days"
    find /tmp -type f -not -path "*/tmux-*" -not -path "*/pytest-*" -not -name "*.sock" \
        -atime +3 -delete 2>/dev/null || true
    mkdir -p "$ALERT_DIR"
    touch "$ALERT_DIR/tmp_warning"
}

clean_sys_red() {
    log WARN "Zone B RED — aggressive /tmp cleanup"
    # Files not accessed in 1+ day
    find /tmp -type f -not -path "*/tmux-*" -not -path "*/pytest-*" -not -name "*.sock" \
        -atime +1 -delete 2>/dev/null || true

    # If still critical, remove all regular files except last 1h, sockets, tmux, pytest
    local pct_after
    pct_after=$(tmp_usage_pct)
    if (( pct_after > 85 )); then
        find /tmp -type f -not -path "*/tmux-*" -not -path "*/pytest-*" -not -name "*.sock" \
            -mmin +60 -delete 2>/dev/null || true
    fi

    mkdir -p "$ALERT_DIR"
    touch "$ALERT_DIR/tmp_emergency"
    log WARN "Zone B RED — aggressive cleanup complete"
}

check_sys_tmp() {
    local pct
    pct=$(tmp_usage_pct)
    local tier="green"

    if (( pct > 85 )); then
        tier="red"
        clean_sys_red
    elif (( pct > 70 )); then
        tier="orange"
        clean_sys_orange
    elif (( pct > 50 )); then
        tier="yellow"
        clean_sys_yellow
    fi

    echo "$tier:$pct"
}

# ── Main loop ────────────────────────────────────────────────
main() {
    mkdir -p "$(dirname "$LOG_FILE")" "$ALERT_DIR"
    log INFO "Watchgod starting (poll=${POLL_INTERVAL}s, budget=${CC_TMP_BUDGET_MB}MB, sacred=${SACRED_GROUND_MB}MB)"

    while true; do
        load_config

        local cc_result sys_result
        cc_result=$(check_cc_tmp)
        sys_result=$(check_sys_tmp)

        local cc_tier="${cc_result%%:*}"
        local cc_used="${cc_result##*:}"
        local sys_tier="${sys_result%%:*}"
        local sys_pct="${sys_result##*:}"

        write_state "$cc_tier" "$cc_used" "$sys_tier" "$sys_pct"

        # Clear stale alerts when back to green
        if [[ "$cc_tier" == "green" && "$sys_tier" == "green" ]]; then
            rm -f "$ALERT_DIR/tmp_warning" "$ALERT_DIR/tmp_emergency" 2>/dev/null || true
        fi

        # Log rotation — truncate when > 1MB
        local log_size
        log_size=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
        if (( log_size > 1048576 )); then
            tail -100 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
        fi

        sleep "$POLL_INTERVAL"
    done
}

main "$@"
