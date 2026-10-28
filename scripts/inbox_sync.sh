#!/bin/bash
# Sync Genesis inbox folder between Dropbox and local VM.
# Runs via cron every 5 minutes.
#
# Flow:
#   1. Pull new files FROM Dropbox → local (two-way sync for responses)
#   2. Detect .genesis.md files deleted by user in Obsidian and clean up locally
#   3. Push .genesis.md responses FROM local → Dropbox
#   4. Record which responses the vault now holds (the evidence step 2 uses)
#
# Response files use .genesis.md suffix (sibling to source files), so a note in
# a subfolder gets its response in that subfolder; steps 2-4 work at any depth.
#
# Local state files (".genesis-*", excluded from the step-1 mirror so the sync
# can never delete its own state, and never pushed):
#   .genesis-lsf-stderr     stderr of the last listing attempt (kept out of the
#                           listing data so error text can never join a
#                           membership decision)
#   .genesis-seen-in-vault  "# remote: <remote>", then the response paths
#                           (relative to the inbox root) the vault held at the
#                           end of the last healthy cycle
#   .genesis-push-filter    this cycle's push filter (rewritten every cycle)

SCRIPT_DIR="$(unset CDPATH; cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DROPBOX_PATH="Apps/remotely-save/1/Genesis"
REMOTE="dropbox:$DROPBOX_PATH"
LOCAL_PATH="${GENESIS_INBOX_PATH:-$HOME/inbox}"
# A trailing slash would make every relative path below start with "/", so no
# local response could ever match the vault listing.
[ "$LOCAL_PATH" != "/" ] && LOCAL_PATH="${LOCAL_PATH%/}"
LOG="${GENESIS_INBOX_SYNC_LOG:-$REPO_DIR/logs/inbox_sync.log}"
# The interpreter that runs `genesis.trash` (overridable for tests).
PYTHON="${GENESIS_PYTHON:-$REPO_DIR/.venv/bin/python}"

mkdir -p "$(dirname "$LOG")" "$LOCAL_PATH"

# Single-flight: an API outage can stretch a cycle past the 5-min cron period
# (rclone retries); overlapping runs would interleave deletions and log lines.
exec 9>"$LOG.lock"
if ! flock -n 9; then
    echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) overlap: previous run still active, skipping ---" >> "$LOG"
    exit 0
fi

# Escape a path for an rclone filter line: glob characters in a file name are
# literal, and the leading "/" anchors it at the inbox root.
_filter_literal() {
    printf '/%s' "$1" | sed -e 's/[][\\*?{}]/\\&/g'
}

# Control chars stripped from audit lines (log injection defense-in-depth);
# UTF-8 titles intact.
_shown() {
    printf '%s' "$1" | tr -d '[:cntrl:]'
}

{
    echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) ---"

    # 1. Pull: Dropbox → local (get new files from Obsidian, skip .genesis.md
    #    so we don't overwrite local responses before push, and skip local
    #    ".genesis-*" state so the mirror never deletes it)
    rclone sync "$REMOTE" "$LOCAL_PATH" \
        --exclude "*.genesis.md" \
        --exclude ".genesis-*" \
        --verbose 2>&1

    # 2. Detect user-deleted .genesis.md files, at any depth.
    #    A local response is a USER DELETION only if a previous healthy listing
    #    saw it in the vault and this healthy listing does not. Absence alone
    #    proves nothing: a response whose upload never landed (a failed push,
    #    or a stretch when this job did not run at all) is also absent, and
    #    deleting it would destroy the only copy. So every healthy cycle
    #    records what the vault held, and only a response named in that record
    #    can be cleaned. A response the vault has never listed is kept and
    #    pushed in step 3.
    #
    #    A cleaned response goes to Genesis's trash (`genesis.trash put`), not
    #    `rm`, so a wrong deletion can be restored. If the trash refuses (the
    #    inbox on another volume, no interpreter), the response is HELD: kept,
    #    left out of this cycle's push so it is not re-uploaded, and kept in
    #    the record so the next cycle tries again. It is never `rm`'d.
    #
    #    A response at a recorded path that now appears elsewhere in the vault
    #    under the same name (outside Obsidian's .trash) was MOVED, not deleted.
    #    It is cleaned the same way and logged as a move.
    #
    #    FAIL-CLOSED (2026-08-24): the listing is the sole evidence for "the
    #    user deleted this". If `rclone lsf` fails (Dropbox API outage, or one
    #    folder it cannot read: lsf -R then exits non-zero, MEASURED on rclone
    #    1.60), an empty or partial listing must NEVER be read as deletions —
    #    on 2026-07-29 exactly that wiped all 150 local response files and
    #    reset the response counter. On a failed listing we skip cleanup, leave
    #    the record untouched, AND hold back the push of >grace-age files
    #    (step 3), so an old local copy of a vault-side deletion cannot be
    #    re-uploaded (resurrected) before the next healthy cycle cleans it.
    #    Bulk deletions on a HEALTHY listing are still honored but logged
    #    loudly.
    #
    #    No usable record (a fresh install, the first cycle after this change,
    #    a record written for a different remote, or a symlink): nothing is
    #    cleaned and the record is seeded from the listing. A response deleted
    #    in the vault in the minutes before that cycle is re-uploaded once;
    #    deleting it again sticks.
    LSF_ERR="$LOCAL_PATH/.genesis-lsf-stderr"
    SEEN="$LOCAL_PATH/.genesis-seen-in-vault"
    FILTER="$LOCAL_PATH/.genesis-push-filter"
    RECORD_HEADER="# remote: $REMOTE"
    PUSH_MAX_AGE=""
    LISTING_OK=""
    HELD=""
    # Never write state through a pre-planted symlink (defense-in-depth;
    # single-tenant box, but rm -f is free and rm does not follow symlinks).
    rm -f -- "$LSF_ERR" "$FILTER"
    if REMOTE_GENESIS=$(rclone lsf -R --files-only "$REMOTE" --include "*.genesis.md" 2>"$LSF_ERR"); then
        LISTING_OK=1
        remote_count=$(printf '%s\n' "$REMOTE_GENESIS" | grep -c . || true)
        cleaned=0
        held=0
        kept_unseen=0
        if [ -f "$SEEN" ] && [ ! -L "$SEEN" ] \
            && [ "$(head -n 1 -- "$SEEN")" = "$RECORD_HEADER" ]; then
            # Local responses at any depth, skipping dot directories (Obsidian's
            # .trash and .obsidian, and this job's own state). Names are DATA:
            # read NUL-delimited, compared with grep -qxF --.
            while IFS= read -r -d '' f; do
                rel="${f#"$LOCAL_PATH"/}"
                case "$rel" in
                    *$'\n'*)
                        # grep -F reads a newline as two patterns, so this name
                        # cannot be matched safely. Keep it; never decide on it.
                        echo "Kept (name contains a newline, not compared): $(_shown "$rel")"
                        continue ;;
                esac
                if printf '%s\n' "$REMOTE_GENESIS" | grep -qxF -- "$rel"; then
                    continue
                fi
                if ! grep -qxF -- "$rel" "$SEEN"; then
                    kept_unseen=$((kept_unseen + 1))
                    continue
                fi
                base="${rel##*/}"
                # ENVIRON, not -v: awk -v would interpret backslashes in a name.
                moved_to=$(printf '%s\n' "$REMOTE_GENESIS" | grep -v '^\.trash/' \
                    | B="$base" awk -F/ '$NF == ENVIRON["B"]' | head -n 1)
                if trash_out=$("$PYTHON" -m genesis.trash put --reason "inbox_sync: response deleted in the vault" -- "$f" 2>&1); then
                    if [ -n "$moved_to" ]; then
                        echo "Moved in vault: $(_shown "$rel") -> $(_shown "$moved_to") (old local copy trashed; responses are never pulled, so the moved copy lives in the vault only)"
                    else
                        echo "Cleaned up (deleted from vault): $(_shown "$rel") (moved to the Genesis trash)"
                    fi
                    cleaned=$((cleaned + 1))
                else
                    echo "HELD (trash refused; kept, not pushed, retried next cycle): $(_shown "$rel") — $(_shown "$trash_out")"
                    HELD="$HELD$rel"$'\n'
                    held=$((held + 1))
                fi
            done < <(find "$LOCAL_PATH" -mindepth 1 -name '.*' -prune -o -type f -name '*.genesis.md' -print0)
            echo "Cleanup pass: vault listing $remote_count response file(s), cleaned $cleaned, held $held, kept $kept_unseen never-listed (will push)"
        else
            echo "Cleanup pass: no usable vault record yet — seeding it from this listing ($remote_count response file(s)); nothing cleaned this cycle"
        fi
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
    #    --ignore-existing: responses are write-once, so a vault copy is never
    #    overwritten (a response the user moved or kept in the vault wins over
    #    a local file that happens to reuse its path).
    #    On a failed-listing cycle PUSH_MAX_AGE gates this to fresh files only
    #    (see step 2) so a not-yet-cleaned deletion can't be re-uploaded.
    #    On a healthy cycle, responses HELD in step 2 are excluded by name.
    if [ -n "$PUSH_MAX_AGE" ]; then
        rclone copy "$LOCAL_PATH" "$REMOTE" \
            --include "*.genesis.md" \
            --ignore-existing \
            --max-age "$PUSH_MAX_AGE" \
            --verbose 2>&1
        push_rc=$?
    else
        {
            while IFS= read -r h; do
                [ -n "$h" ] && printf -- '- %s\n' "$(_filter_literal "$h")"
            done <<< "$HELD"
            printf '%s\n' '+ *.genesis.md' '- **'
        } > "$FILTER"
        rclone copy "$LOCAL_PATH" "$REMOTE" \
            --filter-from "$FILTER" \
            --ignore-existing \
            --verbose 2>&1
        push_rc=$?
    fi
    [ "$push_rc" -ne 0 ] && echo "Push failed (rc=$push_rc); unlanded responses stay unseen and are retried next cycle"

    # 4. Record what the vault holds NOW, after the push, so a response this
    #    cycle uploaded counts as seen: deleting it in the vault before the
    #    next cycle is then a deletion, not an upload to retry. A response
    #    whose upload failed is absent from this listing, so it stays unseen
    #    and is kept and pushed again. Responses HELD in step 2 stay recorded,
    #    so the next cycle retries their cleanup instead of re-pushing them.
    #    The step-2 listing is recorded too: everything in it was seen in the
    #    vault, so a response deleted between the push and this listing still
    #    counts as seen and its deletion is honoured next cycle. If this second
    #    listing fails, the step-2 listing alone is recorded. Never written on
    #    a failed-listing cycle (accepted: a response pushed in such a cycle and
    #    deleted before the next healthy one is re-uploaded once). Written
    #    atomically: a cycle killed mid-write leaves the previous record, never
    #    a truncated one.
    if [ -n "$LISTING_OK" ]; then
        if ! AFTER_PUSH=$(rclone lsf -R --files-only "$REMOTE" --include "*.genesis.md" 2>>"$LSF_ERR"); then
            echo "Record: post-push listing failed; recording the pre-push listing"
            AFTER_PUSH=""
        fi
        rm -f -- "$SEEN.tmp"
        if { printf '%s\n' "$RECORD_HEADER"
             printf '%s\n%s\n%s' "$REMOTE_GENESIS" "$AFTER_PUSH" "$HELD" | sed '/^$/d' | sort -u; } > "$SEEN.tmp"; then
            mv -f -- "$SEEN.tmp" "$SEEN"
        else
            rm -f -- "$SEEN.tmp"
            echo "WARNING: could not write the vault record ($SEEN); the previous record is kept"
        fi
    fi

    echo "--- done ---"
} >> "$LOG" 2>&1
