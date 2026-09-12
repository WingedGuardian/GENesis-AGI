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
#
# Local state files (".genesis-*", excluded from the step-1 mirror so the sync
# can never delete its own state):
#   .genesis-lsf-stderr  stderr of the last listing attempt (kept out of the
#                        listing data so error text can never join a
#                        membership decision)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DROPBOX_PATH="Apps/remotely-save/1/Genesis"
LOCAL_PATH="${GENESIS_INBOX_PATH:-$HOME/inbox}"
LOG="${GENESIS_INBOX_SYNC_LOG:-$REPO_DIR/logs/inbox_sync.log}"

mkdir -p "$(dirname "$LOG")" "$LOCAL_PATH"

# Single-flight: an API outage can stretch a cycle past the 5-min cron period
# (rclone retries); overlapping runs would interleave deletions and log lines.
exec 9>"$LOG.lock"
if ! flock -n 9; then
    echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) overlap: previous run still active, skipping ---" >> "$LOG"
    exit 0
fi

{
    echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) ---"

    # 1. Pull: Dropbox → local (get new files from Obsidian, skip .genesis.md
    #    so we don't overwrite local responses before push, and skip local
    #    ".genesis-*" state so the mirror never deletes it)
    rclone sync "dropbox:$DROPBOX_PATH" "$LOCAL_PATH" \
        --exclude "*.genesis.md" \
        --exclude ".genesis-*" \
        --verbose 2>&1

    # 2. Detect user-deleted .genesis.md files:
    #    A local .genesis.md file >10min old that is missing from a HEALTHY
    #    vault listing was deleted by the user in Obsidian; clean it up locally.
    #    Grace period: cron runs every 5min, so new files push within 5min;
    #    after 10min an unlisted file is treated as a user deletion.
    #
    #    FAIL-CLOSED (2026-08-24): the listing is the sole evidence for "the
    #    user deleted this". If `rclone lsf` fails (Dropbox API outage), an
    #    empty listing must NEVER be read as "everything was deleted" — on
    #    2026-07-29 exactly that wiped all 150 local response files and reset
    #    the response counter. On a failed listing we skip cleanup AND hold
    #    back the push of >grace-age files (step 3), so an old local copy of a
    #    vault-side deletion cannot be re-uploaded (resurrected) before the
    #    next healthy cycle cleans it. Bulk deletions on a HEALTHY listing are
    #    still honored (blocking would resurrect user-deleted files via the
    #    push) but logged loudly.
    #
    #    KNOWN LIMITATION (deferred, tracked): a response whose push never
    #    landed (upload fails while the listing stays healthy) is absent from
    #    the vault WITHOUT being a user deletion, and is reaped after the grace
    #    period. Protecting that edge needs a durable "seen on the vault"
    #    ledger — deferred to its own change. This behavior is no worse than
    #    the pre-2026-08-24 script (which had neither the ledger nor fail-closed).
    LSF_ERR="$LOCAL_PATH/.genesis-lsf-stderr"
    PUSH_MAX_AGE=""
    # Never write state through a pre-planted symlink (defense-in-depth;
    # single-tenant box, but rm -f is free and rm does not follow symlinks).
    rm -f -- "$LSF_ERR"
    if REMOTE_GENESIS=$(rclone lsf "dropbox:$DROPBOX_PATH" --include "*.genesis.md" 2>"$LSF_ERR"); then
        remote_count=$(printf '%s\n' "$REMOTE_GENESIS" | grep -c . || true)
        cleaned=0
        for f in "$LOCAL_PATH"/*.genesis.md; do
            [ -f "$f" ] || continue
            fname=$(basename -- "$f")
            # Filename is DATA, never grep/rm options: grep -qxF -- / rm --.
            if ! printf '%s\n' "$REMOTE_GENESIS" | grep -qxF -- "$fname"; then
                if [ "$(find "$f" -mmin +10 2>/dev/null)" ]; then
                    if rm -- "$f"; then
                        # Control chars stripped from the audit line (log
                        # injection defense-in-depth); UTF-8 titles intact.
                        echo "Cleaned up (deleted from vault): $(printf '%s' "$fname" | tr -d '[:cntrl:]')"
                        cleaned=$((cleaned + 1))
                    fi
                fi
            fi
        done
        echo "Cleanup pass: vault listing $remote_count response file(s), cleaned $cleaned"
        if [ "$cleaned" -gt 5 ]; then
            echo "WARNING: bulk cleanup — $cleaned response files removed in one cycle (vault listing was healthy; verify this matches a deliberate Obsidian cleanup)"
        fi
    else
        lsf_rc=$?
        # Listing unhealthy: hold back the push of >grace-age files so an old
        # local copy of a vault-side deletion cannot be resurrected. Fresh
        # responses (<10min) still ship promptly.
        PUSH_MAX_AGE="10m"
        echo "SKIP cleanup: vault listing failed (rc=$lsf_rc) — refusing to treat an empty listing as mass deletion; holding back push of >${PUSH_MAX_AGE} files. rclone stderr: $(tr -d '[:cntrl:]' < "$LSF_ERR" 2>/dev/null)"
    fi

    # 3. Push: local .genesis.md files → Dropbox (responses back to vault).
    #    On a failed-listing cycle PUSH_MAX_AGE gates this to fresh files only
    #    (see step 2) so a not-yet-cleaned deletion can't be re-uploaded.
    if [ -n "$PUSH_MAX_AGE" ]; then
        rclone copy "$LOCAL_PATH" "dropbox:$DROPBOX_PATH" \
            --include "*.genesis.md" \
            --max-age "$PUSH_MAX_AGE" \
            --verbose 2>&1
    else
        rclone copy "$LOCAL_PATH" "dropbox:$DROPBOX_PATH" \
            --include "*.genesis.md" \
            --verbose 2>&1
    fi

    echo "--- done ---"
} >> "$LOG" 2>&1
