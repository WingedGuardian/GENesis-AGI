#!/usr/bin/env bash
# Phase 6 — pre-commit gate: verify tracked hook changes are accompanied
# by a new line in .genesis-hook-versions.
#
# Runs from scripts/hooks/pre-commit. If a tracked hook is staged for
# commit AND its new hash is NOT in .genesis-hook-versions, block the
# commit with an actionable message.
#
# This enforces release discipline: every hook change is recorded in the
# version history so sync-hooks.sh can distinguish stale-but-known from
# user-modified.
#
# Exit codes:
#     0 — all good (no tracked hook changed, or changes are properly recorded)
#     1 — tracked hook changed without versions file update; commit blocked

set -u
set -o pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSIONS_FILE="$REPO_ROOT/.genesis-hook-versions"

TRACKED_HOOKS=(post-commit pre-commit pre-push)

# Get list of staged files (relative to repo root)
STAGED=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || echo "")

if [[ -z "$STAGED" ]]; then
    exit 0
fi

# For each tracked hook, check if it's staged and if so verify its hash
# is in the versions file.
MISSING=()
for h in "${TRACKED_HOOKS[@]}"; do
    hook_path="scripts/hooks/$h"
    if ! echo "$STAGED" | grep -qxF "$hook_path"; then
        continue  # not staged
    fi

    # Get the STAGED content's hash (not the working-tree version — the user
    # may have unstaged edits on top).
    staged_hash=$(git show ":$hook_path" 2>/dev/null | sha256sum | awk '{print $1}')
    if [[ -z "$staged_hash" ]]; then
        continue
    fi

    if ! grep -qF "$h:$staged_hash" "$VERSIONS_FILE" 2>/dev/null; then
        MISSING+=("$h:$staged_hash")
    fi
done

if [[ ${#MISSING[@]} -eq 0 ]]; then
    exit 0
fi

# Block commit and explain what to do.
echo ""
echo "BLOCKED: Hook change without .genesis-hook-versions update."
echo ""
echo "The following tracked hooks were modified, but their new hashes are"
echo "not recorded in .genesis-hook-versions:"
for entry in "${MISSING[@]}"; do
    echo "  - $entry"
done
echo ""
echo "This file is what sync-hooks.sh consults to distinguish stale Genesis"
echo "installs (safe to auto-update) from user-modified hooks (leave alone)."
echo "Every hook change must be recorded or community installs get stuck."
echo ""
echo "Fix:"
echo "  scripts/update_hook_versions.sh"
echo "  git add .genesis-hook-versions"
echo "  git commit"
echo ""

exit 1
