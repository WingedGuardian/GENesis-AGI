#!/usr/bin/env bash
# Guardian gateway — SSH command dispatcher.
#
# Restricts container->host SSH to exactly these operations:
#   restart-timer  — restart the guardian timer
#   pause          — pause guardian checks
#   resume         — resume guardian checks
#   status         — get current guardian state
#   reset-state    — reset stuck state machine to healthy
#   version        — report CC, code, and Node versions
#   update         — pull latest code + self-update gateway script
#   redeploy <hash> — receive tar archive on stdin, deploy to install dir
#
# SSH authorized_keys entry (replace CONTAINER_IP with your container's IP):
#   command="~/.local/bin/guardian-gateway.sh",from="CONTAINER_IP" ssh-ed25519 ...
#
# This gives Genesis exactly 8 operations on the host. Nothing else.

set -euo pipefail

STATE_DIR="${HOME}/.local/state/genesis-guardian"

case "${SSH_ORIGINAL_COMMAND:-}" in
    restart-timer)
        systemctl --user restart genesis-guardian.timer
        echo '{"ok": true, "action": "restart-timer"}'
        ;;
    pause)
        mkdir -p "$STATE_DIR"
        printf '{"paused": true, "since": "%s"}' "$(date -Is)" > "$STATE_DIR/paused.json"
        echo '{"ok": true, "action": "pause"}'
        ;;
    resume)
        rm -f "$STATE_DIR/paused.json"
        echo '{"ok": true, "action": "resume"}'
        ;;
    status)
        cat "$STATE_DIR/state.json" 2>/dev/null || echo '{"current_state": "unknown"}'
        ;;
    reset-state)
        # Reset Guardian state to HEALTHY when stuck in confirmed_dead/recovering/recovered.
        # Safety: refuses to reset from active investigation states (healthy, confirming, surveying).
        STATE_FILE="$STATE_DIR/state.json"
        if [ ! -f "$STATE_FILE" ]; then
            echo '{"ok": false, "error": "no state file"}' >&2
            exit 1
        fi
        CURRENT=$(python3 -c "import json; print(json.load(open('$STATE_FILE')).get('current_state','unknown'))" 2>/dev/null || echo "unknown")
        case "$CURRENT" in
            confirmed_dead|recovering|recovered)
                # Read-modify-write: preserve any future fields added to state.json
                python3 << PYEOF
import json
from datetime import datetime, timezone
sf = "$STATE_FILE"
with open(sf) as f:
    d = json.load(f)
prev = d.get("current_state", "unknown")
now = datetime.now(timezone.utc).isoformat()
d.update(current_state="healthy", consecutive_failures=0, recheck_count=0,
         first_failure_at=None, recovery_attempts=0, last_healthy_at=now,
         last_check_at=now, auto_reset_count=0, dialogue_sent_at=None,
         dialogue_eta_s=0, dialogue_action=None, cc_unavailable_since=None,
         last_cc_unavailable_alert_at=None)
with open(sf, "w") as f:
    json.dump(d, f, indent=2)
print(json.dumps({"ok": True, "action": "reset-state", "previous_state": prev}))
PYEOF
                ;;
            *)
                printf '{"ok": false, "error": "state is %s, not stuck"}\n' "$CURRENT" >&2
                exit 1
                ;;
        esac
        ;;
    version)
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        CC_VER=$(claude --version 2>/dev/null || echo "unavailable")
        NODE_VER=$(node --version 2>/dev/null || echo "unavailable")
        CODE_VER=$(cd "$INSTALL_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
        CODE_DATE=$(cd "$INSTALL_DIR" && git log -1 --format=%ci 2>/dev/null || echo "unknown")
        DEPLOYED=$(python3 -c "import json; print(json.load(open('$STATE_DIR/deploy_state.json')).get('deployed_commit','unknown'))" 2>/dev/null || echo "unknown")
        printf '{"cc_version": "%s", "node_version": "%s", "code_version": "%s", "code_date": "%s", "deployed_commit": "%s"}\n' \
            "$CC_VER" "$NODE_VER" "$CODE_VER" "$CODE_DATE" "$DEPLOYED"
        ;;
    update)
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        if [ -d "$INSTALL_DIR/.git" ]; then
            cd "$INSTALL_DIR"
            OLD=$(git rev-parse --short HEAD 2>/dev/null)
            # Discard CLAUDE.md before pull — it's a generated file that the
            # repo's version would overwrite with the wrong content (container
            # dev instructions vs Guardian diagnostic instructions). We
            # regenerate it from config/guardian-claude.md after every pull.
            git checkout -- CLAUDE.md 2>/dev/null || true
            # Stash remaining local config changes (container_ip, etc.)
            STASHED=0
            if ! git diff --quiet 2>/dev/null; then
                git stash --quiet 2>/dev/null && STASHED=1
            fi
            if git pull --ff-only 2>/dev/null; then
                # Restore local config changes
                if [ "$STASHED" -eq 1 ]; then
                    if ! git stash pop --quiet 2>/dev/null; then
                        # Conflict — drop the stash and warn (config can be re-set)
                        git checkout -- . 2>/dev/null || true
                        git stash drop --quiet 2>/dev/null || true
                    fi
                fi
                # Regenerate Guardian CLAUDE.md from template (never use repo version)
                if [ -f "$INSTALL_DIR/config/guardian-claude.md" ]; then
                    cp "$INSTALL_DIR/config/guardian-claude.md" "$INSTALL_DIR/CLAUDE.md"
                    # Append per-machine network identity from guardian.yaml
                    _cfg="$INSTALL_DIR/config/guardian.yaml"
                    if [ -f "$_cfg" ]; then
                        _cname=$(grep 'container_name:' "$_cfg" | awk '{print $2}' | tr -d '"' || true)
                        _cip=$(grep 'container_ip:' "$_cfg" | awk '{print $2}' | tr -d '"' || true)
                        _hip=$(grep 'host_ip:' "$_cfg" | awk '{print $2}' | tr -d '"' || true)
                        _hv6=$(ip -6 addr show scope global 2>/dev/null | grep -oP 'inet6 \K[^ /]+' | head -1 || echo '')
                        {
                            echo ""
                            echo "## Network"
                            echo ""
                            echo "- **Container**: ${_cname} at ${_cip}"
                            echo "- **Host VM**: ${_hip} (this machine)"
                            [ -n "$_hv6" ] && echo "- **Host IPv6**: $_hv6"
                            echo "- **Dashboard**: http://${_cip}:5000 (direct, container network)"
                            [ -n "$_hip" ] && echo "               http://${_hip}:5000 (via proxy device)"
                        } >> "$INSTALL_DIR/CLAUDE.md"
                    fi
                fi
                # Self-update: copy gateway script from pulled repo to ~/.local/bin/
                # Safe mid-execution: Linux preserves old inode while this process holds fd.
                # Atomic rename ensures next SSH invocation picks up the new script.
                if [ -f "$INSTALL_DIR/scripts/guardian-gateway.sh" ]; then
                    cp "$INSTALL_DIR/scripts/guardian-gateway.sh" "$HOME/.local/bin/guardian-gateway.sh.new"
                    chmod +x "$HOME/.local/bin/guardian-gateway.sh.new"
                    mv "$HOME/.local/bin/guardian-gateway.sh.new" "$HOME/.local/bin/guardian-gateway.sh"
                fi
                # Restart timer so new check.py code takes effect immediately
                systemctl --user restart genesis-guardian.timer 2>/dev/null || true
                NEW=$(git rev-parse --short HEAD 2>/dev/null)
                printf '{"ok": true, "action": "update", "old": "%s", "new": "%s"}\n' "$OLD" "$NEW"
            else
                [ "$STASHED" -eq 1 ] && git stash pop --quiet 2>/dev/null || true
                printf '{"ok": false, "action": "update", "error": "git pull failed"}\n' >&2
                exit 1
            fi
        else
            echo '{"ok": false, "action": "update", "error": "not a git repo"}' >&2
            exit 1
        fi
        ;;
    redeploy\ *)
        # Push-based redeploy: container sends tar archive on stdin.
        # Usage: tar ... | ssh host "redeploy <commit_hash>"
        # The container is the source of truth — no git pull needed.
        COMMIT_HASH="${SSH_ORIGINAL_COMMAND#redeploy }"
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        BACKUP_DIR="${STATE_DIR}/deploy-backup"

        # Validate commit hash (must be 7-40 hex chars — defense in depth)
        if ! echo "$COMMIT_HASH" | grep -qE '^[0-9a-f]{7,40}$'; then
            echo '{"ok": false, "action": "redeploy", "error": "invalid commit hash"}' >&2
            exit 1
        fi

        # Stop timer during extraction to prevent running on partial state
        systemctl --user stop genesis-guardian.timer 2>/dev/null || true

        # Backup current installation for rollback
        rm -rf "$BACKUP_DIR"
        if [ -d "$INSTALL_DIR/src" ]; then
            cp -a "$INSTALL_DIR" "$BACKUP_DIR"
        fi

        # Extract archive from stdin into install dir
        mkdir -p "$INSTALL_DIR"
        if ! tar -xf - -C "$INSTALL_DIR" 2>/dev/null; then
            # Rollback on extraction failure
            if [ -d "$BACKUP_DIR" ]; then
                rm -rf "$INSTALL_DIR"
                mv "$BACKUP_DIR" "$INSTALL_DIR"
            fi
            # Restart timer with old code
            systemctl --user start genesis-guardian.timer 2>/dev/null || true
            echo '{"ok": false, "action": "redeploy", "error": "tar extraction failed"}' >&2
            exit 1
        fi

        # Self-update gateway script (atomic rename — safe mid-execution)
        if [ -f "$INSTALL_DIR/scripts/guardian-gateway.sh" ]; then
            cp "$INSTALL_DIR/scripts/guardian-gateway.sh" "$HOME/.local/bin/guardian-gateway.sh.new"
            chmod +x "$HOME/.local/bin/guardian-gateway.sh.new"
            mv "$HOME/.local/bin/guardian-gateway.sh.new" "$HOME/.local/bin/guardian-gateway.sh"
        fi

        # Regenerate CLAUDE.md from template (never use repo version on host)
        if [ -f "$INSTALL_DIR/config/guardian-claude.md" ]; then
            cp "$INSTALL_DIR/config/guardian-claude.md" "$INSTALL_DIR/CLAUDE.md"
            _cfg="$INSTALL_DIR/config/guardian.yaml"
            if [ -f "$_cfg" ]; then
                # || true: guardian.yaml is host-specific; some fields may be absent
                _cname=$(grep 'container_name:' "$_cfg" 2>/dev/null | awk '{print $2}' | tr -d '"' || true)
                _cip=$(grep 'container_ip:' "$_cfg" 2>/dev/null | awk '{print $2}' | tr -d '"' || true)
                _hip=$(grep 'host_ip:' "$_cfg" 2>/dev/null | awk '{print $2}' | tr -d '"' || true)
                {
                    echo ""
                    echo "## Network"
                    echo ""
                    [ -n "$_cname" ] && echo "- **Container**: ${_cname} at ${_cip}"
                    [ -n "$_hip" ] && echo "- **Host VM**: ${_hip} (this machine)"
                    [ -n "$_cip" ] && echo "- **Dashboard**: http://${_cip}:5000"
                } >> "$INSTALL_DIR/CLAUDE.md"
            fi
        fi

        # Record deployed commit (separate file — state.json is overwritten by Guardian ticks)
        mkdir -p "$STATE_DIR"
        python3 << PYEOF
import json
from datetime import datetime, timezone
sf = "$STATE_DIR/deploy_state.json"
d = {"deployed_commit": "$COMMIT_HASH", "deployed_at": datetime.now(timezone.utc).isoformat()}
with open(sf, "w") as f:
    json.dump(d, f, indent=2)
print(json.dumps({"ok": True, "action": "redeploy", "commit": "$COMMIT_HASH"}))
PYEOF

        # Restart timer so new code takes effect immediately
        systemctl --user restart genesis-guardian.timer 2>/dev/null || true

        # Clean up backup on success
        rm -rf "$BACKUP_DIR"
        ;;
    ping)
        echo '{"ok": true, "action": "ping"}'
        ;;
    *)
        echo '{"ok": false, "error": "denied"}' >&2
        exit 1
        ;;
esac
