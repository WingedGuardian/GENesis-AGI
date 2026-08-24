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
# Local state files (all ".genesis-*", excluded from the step-1 mirror so the
# sync can never delete its own state — the old in-mirror response counter was
# deleted every cycle for months before this was caught):
#   .genesis-seen        names ever seen in a HEALTHY vault listing (deletion
#                        evidence: only a file the vault once had can be
#                        "deleted from the vault")
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
    #    If a local .genesis.md file is >10min old, was seen on the vault at
    #    least once (a push we KNOW succeeded), but is missing from a HEALTHY
    #    vault listing — the user deleted it in Obsidian; clean up locally too.
    #    Grace period: cron runs every 5min, so new files get pushed within
    #    5min; after 10min an unlisted-but-seen file must be a user deletion.
    #
    #    FAIL-CLOSED (2026-08-24): the listing below is the sole evidence for
    #    "the user deleted this". If `rclone lsf` fails (Dropbox API outage),
    #    an empty listing must NEVER be read as "everything was deleted" —
    #    on 2026-07-29 exactly that wiped all 150 local response files and
    #    reset the response counter. A skipped cleanup cycle is harmless
    #    (the next healthy cycle catches real deletions); a false-positive
    #    wipe is not. The .genesis-seen requirement closes the sibling hole:
    #    a response the push step never landed (quota/upload failures while
    #    the listing stays healthy) is absent from the vault WITHOUT being a
    #    user deletion — it must be retried, not reaped. Bulk deletions on a
    #    HEALTHY listing are still honored (a blocked bulk delete would
    #    resurrect user-deleted files via the push in step 3) but logged
    #    loudly for the audit trail.
    LSF_ERR="$LOCAL_PATH/.genesis-lsf-stderr"
    SEEN="$LOCAL_PATH/.genesis-seen"
    # Never write state through a pre-planted symlink (defense-in-depth;
    # single-tenant box, but rm -f is free and rm does not follow symlinks)
    rm -f -- "$LSF_ERR" "$SEEN.tmp"
    if REMOTE_GENESIS=$(rclone lsf "dropbox:$DROPBOX_PATH" --include "*.genesis.md" 2>"$LSF_ERR"); then
        remote_count=$(printf '%s\n' "$REMOTE_GENESIS" | grep -c . || true)
        # Record every vault-listed name as "seen on the vault at least once"
        { [ -f "$SEEN" ] && cat "$SEEN"; printf '%s\n' "$REMOTE_GENESIS"; } \
            | grep . | LC_ALL=C sort -u > "$SEEN.tmp" && mv "$SEEN.tmp" "$SEEN"
        cleaned=0
        for f in "$LOCAL_PATH"/*.genesis.md; do
            [ -f "$f" ] || continue
            fname=$(basename -- "$f")
            if ! printf '%s\n' "$REMOTE_GENESIS" | grep -qxF -- "$fname"; then
                # Absent from the vault: only a user deletion if the vault
                # ever had it — otherwise it's an un-landed push (keep and
                # let step 3 retry it).
                if grep -qxF -- "$fname" "$SEEN" 2>/dev/null \
                    && [ "$(find "$f" -mmin +10 2>/dev/null)" ]; then
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
        echo "SKIP cleanup: vault listing failed (rc=$lsf_rc) — refusing to treat an empty listing as mass deletion. rclone stderr: $(tr -d '[:cntrl:]' < "$LSF_ERR" 2>/dev/null)"
    fi

    # 3. Push: local .genesis.md files → Dropbox (responses back to vault)
    rclone copy "$LOCAL_PATH" "dropbox:$DROPBOX_PATH" \
        --include "*.genesis.md" \
        --verbose 2>&1

    echo "--- done ---"
} >> "$LOG" 2>&1
