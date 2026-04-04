#!/bin/bash
# Push a prepared public release to the GENesis-AGI repo.
# Merges into the existing repo (preserving public-only files like README).
# Optionally tags the release and creates a GitHub Release.
# NEVER force pushes.
#
# Usage:
#   ./scripts/push-public-release.sh <staging-dir> [--version vX.Y] [commit-message]
#
# Examples:
#   ./scripts/push-public-release.sh ~/tmp/genesis-public-release/
#   ./scripts/push-public-release.sh ~/tmp/genesis-public-release/ --version v3.0a
#
# Prerequisites:
#   - gh auth login (GitHub CLI authenticated)
#   - staging dir from prepare-public-release.sh
#   - CHANGELOG.md with a [vX.Y] section (when using --version)

set -euo pipefail

PUBLIC_REPO="WingedGuardian/GENesis-AGI"
WORK_DIR="$(mktemp -d)/genesis-agi-push"

# Files where the PUBLIC repo version is authoritative.
PUBLIC_AUTHORITATIVE=(
    "README.md"
)

# ── Argument parsing ───────────────────────────────────────
STAGING_DIR=""
VERSION=""
COMMIT_MSG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            VERSION="${2:-}"
            [[ -z "$VERSION" ]] && { echo "ERROR: --version requires a value (e.g. v3.0a)"; exit 1; }
            shift 2
            ;;
        -*)
            echo "Unknown flag: $1"
            exit 1
            ;;
        *)
            if [[ -z "$STAGING_DIR" ]]; then
                STAGING_DIR="$1"
            elif [[ -z "$COMMIT_MSG" ]]; then
                COMMIT_MSG="$1"
            fi
            shift
            ;;
    esac
done

if [[ -z "$STAGING_DIR" ]]; then
    echo "Usage: $0 <staging-dir> [--version vX.Y] [commit-message]"
    exit 1
fi

if [[ ! -f "$STAGING_DIR/.genesis-source-commit" ]]; then
    echo "ERROR: $STAGING_DIR doesn't look like a release staging dir."
    echo "       Missing .genesis-source-commit marker."
    exit 1
fi

# Default commit message
if [[ -z "$COMMIT_MSG" ]]; then
    if [[ -n "$VERSION" ]]; then
        COMMIT_MSG="Genesis $VERSION — public release"
    else
        COMMIT_MSG="Genesis v3 — public release update"
    fi
fi

SOURCE_COMMIT=$(cat "$STAGING_DIR/.genesis-source-commit")
echo "=== Push Public Release ==="
echo "  Staging: $STAGING_DIR"
echo "  Source commit: $SOURCE_COMMIT"
echo "  Target: $PUBLIC_REPO"
[[ -n "$VERSION" ]] && echo "  Version: $VERSION"
echo

# ── Validate CHANGELOG when releasing ─────────────────────
CHANGELOG_NOTES=""
if [[ -n "$VERSION" ]]; then
    CHANGELOG="$STAGING_DIR/CHANGELOG.md"
    if [[ ! -f "$CHANGELOG" ]]; then
        echo "ERROR: CHANGELOG.md not found in staging dir."
        echo "       Run prepare-public-release.sh first, then ensure CHANGELOG.md is committed."
        exit 1
    fi
    # Extract the release notes section for VERSION (skip heading, stop at next ## [)
    # Pure awk: avoids GNU-specific `head -n -1` (macOS BSD head doesn't support it)
    CHANGELOG_NOTES=$(awk "/^## \[$VERSION\]/{found=1; next} found && /^## \[/{exit} found{print}" "$CHANGELOG" | sed '/^$/d' | head -50)
    if [[ -z "$CHANGELOG_NOTES" ]]; then
        echo "ERROR: No section for '$VERSION' found in CHANGELOG.md."
        echo "       Expected a heading like: ## [$VERSION] - YYYY-MM-DD"
        echo "       Run ./scripts/draft-changelog.sh to generate a draft, then curate."
        exit 1
    fi
    echo "  Release notes: $(echo "$CHANGELOG_NOTES" | wc -l) lines"
    echo
fi

# ── Clone existing public repo ─────────────────────────────
echo "--- Cloning $PUBLIC_REPO ---"
git clone "https://github.com/$PUBLIC_REPO.git" "$WORK_DIR" 2>&1
cd "$WORK_DIR"
git config user.email "noreply@github.com"
git config user.name "Genesis Release"
echo

# ── Identify files to preserve ────────────────────────────
echo "--- Identifying files to preserve ---"
PRESERVE_FILES=()

for f in "${PUBLIC_AUTHORITATIVE[@]}"; do
    if [[ -f "$f" ]]; then
        PRESERVE_FILES+=("$f")
        echo "  Preserving (authoritative): $f"
    fi
done

while IFS= read -r -d '' file; do
    rel="${file#./}"
    if [[ ! -e "$STAGING_DIR/$rel" ]]; then
        PRESERVE_FILES+=("$rel")
        echo "  Preserving (public-only): $rel"
    fi
done < <(find . -not -path './.git/*' -not -path './.git' -not -name '.' -type f -print0)
echo "  ${#PRESERVE_FILES[@]} file(s) to preserve"
echo

# ── Save preserved files ──────────────────────────────────
SAVE_DIR="$(mktemp -d)"
trap 'rm -rf "$SAVE_DIR"' EXIT
for f in "${PRESERVE_FILES[@]}"; do
    mkdir -p "$SAVE_DIR/$(dirname "$f")"
    cp "$f" "$SAVE_DIR/$f"
done

# ── Replace repo content with staging ────────────────────
echo "--- Replacing repo content ---"
git ls-files -z | xargs -0 rm -f 2>/dev/null || true
find . -not -path './.git/*' -not -path './.git' -type d -empty -delete 2>/dev/null || true
cp -a "$STAGING_DIR/." .
for f in "${PRESERVE_FILES[@]}"; do
    mkdir -p "$(dirname "$f")"
    cp "$SAVE_DIR/$f" "$f"
done
echo

# ── Commit and push ───────────────────────────────────────
echo "--- Committing ---"
git add -A
CHANGES=$(git diff --cached --stat)
if [[ -z "$CHANGES" ]]; then
    if [[ -n "$VERSION" ]]; then
        echo "  No file changes — content already up to date."
        echo "  Proceeding to tag and release..."
    else
        echo "  No changes to push. Public repo is up to date."
        rm -rf "$WORK_DIR" "$SAVE_DIR"
        exit 0
    fi
else
    echo "$CHANGES" | tail -3

    git commit -m "$COMMIT_MSG

Source: $SOURCE_COMMIT

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"

    echo
    echo "--- Pushing (normal push, never force) ---"
    if ! git push origin main 2>&1; then
        echo
        echo "ERROR: Push failed. Remote has commits not in this staging."
        echo "       Resolve manually: cd $WORK_DIR && git pull --rebase origin main && git push"
        exit 1
    fi
fi

# ── Tag and GitHub Release ────────────────────────────────
if [[ -n "$VERSION" ]]; then
    echo
    echo "--- Creating release $VERSION ---"

    # Annotated tag — idempotent: check remote (fresh clone has no local tags)
    if git ls-remote --tags origin "$VERSION" | grep -q .; then
        echo "  Tag $VERSION already exists on remote — skipping tag creation"
    else
        git tag -a "$VERSION" -m "Genesis $VERSION"
        git push origin "$VERSION"
        echo "  Tag pushed: $VERSION"
    fi

    # GitHub Release — idempotent: skip if already exists
    NOTES_FILE="$(mktemp)"
    trap 'rm -f "$NOTES_FILE"' EXIT
    printf '%s\n' "$CHANGELOG_NOTES" > "$NOTES_FILE"

    if gh release view "$VERSION" --repo "$PUBLIC_REPO" &>/dev/null; then
        echo "  GitHub Release $VERSION already exists — skipping"
    else
        gh release create "$VERSION" \
            --repo "$PUBLIC_REPO" \
            --title "Genesis $VERSION" \
            --notes-file "$NOTES_FILE"
        echo "  GitHub Release created: https://github.com/$PUBLIC_REPO/releases/tag/$VERSION"
    fi
fi

echo
echo "=== Push complete ==="
echo "  Repo: https://github.com/$PUBLIC_REPO"
echo "  Files preserved: ${#PRESERVE_FILES[@]}"

rm -rf "$SAVE_DIR"
echo "  Work dir: $WORK_DIR (kept for inspection)"
