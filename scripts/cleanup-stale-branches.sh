#!/usr/bin/env bash
# cleanup-stale-branches.sh — detect (and optionally delete) local branches
# whose content is fully on main.
#
# Two passes:
#   1. Any branch whose tip is reachable from `main` (git merge-base
#      --is-ancestor) is stale — its commits are all on main. Safe to delete.
#   2. `worktree-*` branches whose worktree directory no longer exists, AND
#      whose tip is reachable from main, are orphaned. Safe to delete.
#
# Usage:
#   scripts/cleanup-stale-branches.sh           # dry-run (report only)
#   scripts/cleanup-stale-branches.sh --auto-delete
#
# Safety: never deletes branches with unique commits (those are flagged for
# manual review). Uses `git branch -D` only on branches that pass the
# is-ancestor check. The 30-day `git reflog` grace period remains.
#
# Invoked:
#   - On-demand by user
#   - Automatically by scripts/hooks/post-commit when a public-release
#     commit subject is detected
#   - Can be cron'd or run from the surplus scheduler

set -euo pipefail

AUTO_DELETE="${1:-}"
MAIN_BRANCH="${MAIN_BRANCH:-main}"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "error: not inside a git repository" >&2
    exit 1
fi

if ! git show-ref --verify --quiet "refs/heads/$MAIN_BRANCH"; then
    echo "error: no local '$MAIN_BRANCH' branch" >&2
    exit 1
fi

# Collect the set of branches that are currently checked out in any worktree —
# these cannot be deleted (git refuses with "checked out at ...").
CHECKED_OUT="$(git worktree list --porcelain 2>/dev/null | awk '/^branch refs\/heads\// {sub(/^branch refs\/heads\//, ""); print}' | sort -u)"
is_checked_out() {
    printf '%s\n' "$CHECKED_OUT" | grep -Fxq "$1"
}

stale_count=0
unique_count=0
orphan_count=0
skipped_checked_out=0

echo "=== Pass 1: branches fully merged into $MAIN_BRANCH ==="
while IFS= read -r branch; do
    [ -z "$branch" ] && continue
    [ "$branch" = "$MAIN_BRANCH" ] && continue

    if git merge-base --is-ancestor "$branch" "$MAIN_BRANCH" 2>/dev/null; then
        if is_checked_out "$branch"; then
            echo "SKIP (checked out): $branch"
            skipped_checked_out=$((skipped_checked_out + 1))
            continue
        fi
        echo "STALE (fully merged): $branch"
        stale_count=$((stale_count + 1))
        if [ "$AUTO_DELETE" = "--auto-delete" ]; then
            git branch -D "$branch" >/dev/null 2>&1 && echo "  → deleted" || echo "  → delete failed"
        fi
    else
        unique=$(git log --format='%H' "$MAIN_BRANCH..$branch" 2>/dev/null | wc -l | tr -d ' ')
        if [ "$unique" -gt 0 ]; then
            echo "KEEP  ($unique unique commits): $branch"
            unique_count=$((unique_count + 1))
        fi
    fi
done < <(git branch --format='%(refname:short)')

echo ""
echo "=== Pass 2: orphan worktree-* branches (worktree dir gone) ==="
while IFS= read -r branch; do
    [ -z "$branch" ] && continue
    case "$branch" in worktree-*) ;; *) continue ;; esac

    # Is there a worktree currently pointing at this branch?
    if is_checked_out "$branch"; then
        continue  # active worktree, leave it alone
    fi

    # No worktree; is the branch fully on main?
    if git merge-base --is-ancestor "$branch" "$MAIN_BRANCH" 2>/dev/null; then
        echo "ORPHAN WORKTREE BRANCH: $branch"
        orphan_count=$((orphan_count + 1))
        if [ "$AUTO_DELETE" = "--auto-delete" ]; then
            git branch -D "$branch" >/dev/null 2>&1 && echo "  → deleted" || echo "  → delete failed"
        fi
    fi
done < <(git branch --format='%(refname:short)')

echo ""
echo "=== Summary ==="
echo "Stale (merged) branches:      $stale_count"
echo "Orphan worktree-* branches:   $orphan_count"
echo "Branches with unique commits: $unique_count  (kept — review manually)"
echo "Skipped (checked out):        $skipped_checked_out"
if [ "$AUTO_DELETE" != "--auto-delete" ]; then
    echo ""
    echo "Dry run. Re-run with --auto-delete to actually remove stale branches."
fi
