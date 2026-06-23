#!/usr/bin/env bash
# Manage ONLY the Genesis backup cron line.
#
# Single-line surgery via the read-filter-reinstall idiom so other crontab
# entries (inbox_sync, etc.) are never clobbered. Called by the dashboard
# Backup-config endpoint instead of having a web process edit the crontab
# inline — keeps the web surface narrow and the cron management auditable.
#
# Usage:
#   manage_backup_cron.sh install "<5-field cron expr>"   # add/replace
#   manage_backup_cron.sh remove                          # delete the line
#   manage_backup_cron.sh show                            # print it (or nothing)
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SCRIPT="$_SCRIPT_DIR/backup.sh"
LOG_FILE="$HOME/genesis/logs/backup.log"
ACTION="${1:-}"

# Current crontab with any backup.sh line removed (empty if no crontab yet).
_crontab_without_backup() {
    crontab -l 2>/dev/null | grep -vE 'backup\.sh' || true
}

case "$ACTION" in
    install)
        SCHED="${2:?cron schedule required}"
        # Defence in depth (the caller validates too): exactly 5 fields, and
        # only the safe cron charset — no shell metacharacters, no newlines.
        if [ "$(printf '%s' "$SCHED" | awk '{print NF}')" -ne 5 ]; then
            echo "invalid cron schedule: expected 5 fields, got: $SCHED" >&2
            exit 2
        fi
        if printf '%s' "$SCHED" | grep -qE '[^0-9*,/ -]'; then
            echo "invalid cron schedule: illegal characters in: $SCHED" >&2
            exit 2
        fi
        LINE="$SCHED $BACKUP_SCRIPT >> $LOG_FILE 2>&1"
        { _crontab_without_backup; printf '%s\n' "$LINE"; } | crontab -
        echo "installed: $LINE"
        ;;
    remove)
        _crontab_without_backup | crontab -
        echo "removed"
        ;;
    show)
        crontab -l 2>/dev/null | grep -E 'backup\.sh' || true
        ;;
    *)
        echo "usage: $0 {install <cron-expr>|remove|show}" >&2
        exit 2
        ;;
esac
