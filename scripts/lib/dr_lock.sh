# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# Shared backup↔restore mutual-exclusion lock constants + open helper (SF5).
# Single-sourced by scripts/backup.sh and scripts/restore.sh so the lock PATH
# and fd can never drift between them — if they named different files or fds,
# both would acquire "successfully" and run concurrently, silently
# reintroducing the exact hole SF5 closes (a 6h backup snapshotting a
# half-restored DB as the newest COMPLETE).
#
#   dr_lock_open   — mkdir the lock dir and open the lock file on DR_LOCK_FD in
#                    APPEND mode (never O_TRUNC: a losing contender must not
#                    truncate the winner's holder line). Does NOT acquire —
#                    the caller runs `flock -n`/`flock -w` per its own policy
#                    (backup skips, restore waits).
#   dr_lock_stamp <role> — after acquiring, record "<pid> <role> <iso8601>" so
#                    the other side's timeout/skip message can name the holder.

DR_LOCK_DIR="${GENESIS_HOME:-$HOME/.genesis}/locks"
DR_LOCK_FILE="$DR_LOCK_DIR/backup-restore.lock"
DR_LOCK_FD=200

dr_lock_open() {
    mkdir -p "$DR_LOCK_DIR"
    # Open on the fixed fd in append mode (no truncate-on-open).
    eval "exec ${DR_LOCK_FD}>>\"\$DR_LOCK_FILE\""
}

dr_lock_stamp() {
    printf '%s %s %s\n' "$$" "$1" "$(date -Iseconds)" > "$DR_LOCK_FILE"
}
