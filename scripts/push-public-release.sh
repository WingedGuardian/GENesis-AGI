#!/bin/bash
# Push a prepared public release to the GENesis-AGI repo.
# Merges into the existing repo (preserving public-only files like README).
# NEVER force pushes.
#
# Usage: ./scripts/push-public-release.sh <staging-dir> [commit-message]
#
# Prerequisites:
#   - gh auth login (GitHub CLI authenticated)
#   - staging dir from prepare-public-release.sh

set -euo pipefail

STAGING_DIR="${1:-}"
COMMIT_MSG="${2:-Genesis v3 — public release update}"
PUBLIC_REPO="YOUR_GITHUB_USER/genesis-AGI"
WORK_DIR="$(mktemp -d)/genesis-agi-push"

if [[ -z "$STAGING_DIR" ]]; then
    echo "Usage: $0 <staging-dir> [commit-message]"
    exit 1
fi

if [[ ! -f "$STAGING_DIR/.genesis-source-commit" ]]; then
    echo "ERROR: $STAGING_DIR doesn't look like a release staging dir."
    echo "       Missing .genesis-source-commit marker."
    exit 1
fi

SOURCE_COMMIT=$(cat "$STAGING_DIR/.genesis-source-commit")
echo "=== Push Public Release ==="
echo "  Staging: $STAGING_DIR"
echo "  Source commit: $SOURCE_COMMIT"
echo "  Target: $PUBLIC_REPO"
echo

# --- Clone existing public repo ---
echo "--- Cloning $PUBLIC_REPO ---"
git clone "https://github.com/$PUBLIC_REPO.git" "$WORK_DIR" 2>&1
cd "$WORK_DIR"
git config user.email "noreply@github.com"
git config user.name "Genesis Release"
echo

# --- Identify public-only files (exist in repo but not in staging) ---
echo "--- Identifying public-only files ---"
PUBLIC_ONLY=()
while IFS= read -r -d '' file; do
    rel="${file#./}"
    if [[ ! -e "$STAGING_DIR/$rel" ]]; then
        PUBLIC_ONLY+=("$rel")
        echo "  Preserving: $rel"
    fi
done < <(find . -not -path './.git/*' -not -path './.git' -not -name '.' -type f -print0)
echo "  ${#PUBLIC_ONLY[@]} public-only file(s) found"
echo

# --- Save public-only files ---
SAVE_DIR="$(mktemp -d)"
for f in "${PUBLIC_ONLY[@]}"; do
    mkdir -p "$SAVE_DIR/$(dirname "$f")"
    cp "$f" "$SAVE_DIR/$f"
done

# --- Replace repo content with staging ---
echo "--- Replacing repo content ---"
# Remove all tracked files
git ls-files -z | xargs -0 rm -f 2>/dev/null || true
# Remove empty directories
find . -not -path './.git/*' -not -path './.git' -type d -empty -delete 2>/dev/null || true
# Copy staging content
cp -a "$STAGING_DIR/." .
# Restore public-only files
for f in "${PUBLIC_ONLY[@]}"; do
    mkdir -p "$(dirname "$f")"
    cp "$SAVE_DIR/$f" "$f"
done
echo

# --- Commit and push ---
echo "--- Committing ---"
git add -A
CHANGES=$(git diff --cached --stat)
if [[ -z "$CHANGES" ]]; then
    echo "  No changes to push. Public repo is up to date."
    rm -rf "$WORK_DIR" "$SAVE_DIR"
    exit 0
fi
echo "$CHANGES" | tail -3

git commit -m "$COMMIT_MSG

Source: $SOURCE_COMMIT

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"

echo
echo "--- Pushing (normal push, never force) ---"
if ! git push origin main 2>&1; then
    echo
    echo "ERROR: Push failed. This likely means the remote has commits not"
    echo "       in this staging. Pull and resolve manually:"
    echo "       cd $WORK_DIR && git pull --rebase origin main && git push"
    exit 1
fi

echo
echo "=== Push complete ==="
echo "  Repo: https://github.com/$PUBLIC_REPO"
echo "  Files preserved: ${#PUBLIC_ONLY[@]}"

# Cleanup
rm -rf "$SAVE_DIR"
echo "  Work dir: $WORK_DIR (kept for inspection)"
