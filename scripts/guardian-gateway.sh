#!/usr/bin/env bash
# Guardian gateway — SSH command dispatcher.
#
# Restricts container->host SSH to exactly these operations:
#   restart-timer  — restart the guardian timer
#   pause          — pause guardian checks
#   resume         — resume guardian checks
#   status         — get current guardian state
#   version        — report CC, code, and Node versions
#   update         — pull latest code from git
#
# SSH authorized_keys entry (replace CONTAINER_IP with your container's IP):
#   command="~/.local/bin/guardian-gateway.sh",from="CONTAINER_IP" ssh-ed25519 ...
#
# This gives Genesis exactly 4 operations on the host. Nothing else.

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
    version)
        INSTALL_DIR="${HOME}/.local/share/genesis-guardian"
        CC_VER=$(claude --version 2>/dev/null || echo "unavailable")
        NODE_VER=$(node --version 2>/dev/null || echo "unavailable")
        CODE_VER=$(cd "$INSTALL_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
        CODE_DATE=$(cd "$INSTALL_DIR" && git log -1 --format=%ci 2>/dev/null || echo "unknown")
        printf '{"cc_version": "%s", "node_version": "%s", "code_version": "%s", "code_date": "%s"}\n' \
            "$CC_VER" "$NODE_VER" "$CODE_VER" "$CODE_DATE"
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
                fi
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
    *)
        echo '{"ok": false, "error": "denied"}' >&2
        exit 1
        ;;
esac
