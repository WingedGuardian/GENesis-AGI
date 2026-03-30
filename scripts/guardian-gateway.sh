#!/usr/bin/env bash
# Guardian gateway — SSH command dispatcher.
#
# Restricts container->host SSH to exactly these operations:
#   restart-timer  — restart the guardian timer
#   pause          — pause guardian checks
#   resume         — resume guardian checks
#   status         — get current guardian state
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
    *)
        echo '{"ok": false, "error": "denied"}' >&2
        exit 1
        ;;
esac
