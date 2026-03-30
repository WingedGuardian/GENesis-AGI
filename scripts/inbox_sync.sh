#!/bin/bash
# Sync Genesis inbox folder between Dropbox and local VM.
# Runs via cron every 5 minutes.
#
# Flow:
#   1. Pull new files FROM Dropbox → local (two-way sync for responses)
#   2. Detect .genesis.md files deleted by user in Obsidian and clean up locally
#   3. Push .genesis.md responses FROM local → Dropbox
#
# Response files use .genesis.md suffix (sibling to source files).
# No subdirectory needed — everything lives in the same folder.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DROPBOX_PATH="Apps/remotely-save/1/Genesis"
LOCAL_PATH="${GENESIS_INBOX_PATH:-$HOME/inbox}"
LOG="${GENESIS_INBOX_SYNC_LOG:-$REPO_DIR/logs/inbox_sync.log}"

mkdir -p "$(dirname "$LOG")" "$LOCAL_PATH"

{
    echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) ---"

    # 1. Pull: Dropbox → local (get new files from Obsidian, skip .genesis.md
    #    so we don't overwrite local responses before push)
    rclone sync "dropbox:$DROPBOX_PATH" "$LOCAL_PATH" \
        --exclude "*.genesis.md" \
        --verbose 2>&1

    # 2. Detect user-deleted .genesis.md files:
    #    If a local .genesis.md file is >10min old (already pushed at least once)
    #    but missing from Dropbox, the user deleted it — clean up locally too.
    #    Grace period: cron runs every 5min, so new files get pushed within 5min.
    #    After 10min, if still not on Dropbox, user must have deleted it.
    REMOTE_GENESIS=$(rclone lsf "dropbox:$DROPBOX_PATH" --include "*.genesis.md" 2>/dev/null || true)
    for f in "$LOCAL_PATH"/*.genesis.md; do
        [ -f "$f" ] || continue
        fname=$(basename "$f")
        if ! echo "$REMOTE_GENESIS" | grep -qxF "$fname"; then
            if [ "$(find "$f" -mmin +10 2>/dev/null)" ]; then
                echo "Cleaned up (deleted from vault): $fname"
                rm "$f"
            fi
        fi
    done

    # 3. Push: local .genesis.md files → Dropbox (responses back to vault)
    rclone copy "$LOCAL_PATH" "dropbox:$DROPBOX_PATH" \
        --include "*.genesis.md" \
        --verbose 2>&1

    echo "--- done ---"
} >> "$LOG" 2>&1
