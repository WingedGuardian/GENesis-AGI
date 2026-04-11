#!/usr/bin/env bash
# Phase 6 — append current hook hashes to .genesis-hook-versions.
#
# Idempotent: lines already present are skipped. Adds new lines for hashes
# that don't yet exist in the file.
#
# Run this whenever you modify a tracked hook (post-commit, pre-commit,
# pre-push) and need to record the new version. The pre-commit gate
# (scripts/check_hook_versions.sh) will refuse to commit a hook change
# without a corresponding entry here.
#
# Usage:
#     scripts/update_hook_versions.sh
#
# Output: prints which entries were added. Exit 0 always (nothing to do
# is not an error).

set -u
set -o pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSIONS_FILE="$REPO_ROOT/.genesis-hook-versions"

# Hooks we track.
TRACKED_HOOKS=(post-commit pre-commit pre-push)

if [[ ! -f "$VERSIONS_FILE" ]]; then
    echo "ERROR: $VERSIONS_FILE not found." >&2
    echo "This file is the source of truth for hook version history." >&2
    exit 1
fi

ADDED=0
for h in "${TRACKED_HOOKS[@]}"; do
    src="$REPO_ROOT/scripts/hooks/$h"
    if [[ ! -f "$src" ]]; then
        continue  # tracked but not yet present — skip silently
    fi
    hash=$(sha256sum "$src" | awk '{print $1}')
    entry="$h:$hash"
    if grep -qF "$entry" "$VERSIONS_FILE"; then
        continue  # already recorded
    fi
    echo "$entry" >> "$VERSIONS_FILE"
    echo "added: $entry"
    ADDED=$((ADDED + 1))
done

if [[ $ADDED -eq 0 ]]; then
    echo "no changes — all tracked hook hashes already present"
fi

exit 0
