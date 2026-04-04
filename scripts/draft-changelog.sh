#!/bin/bash
# Draft an [Unreleased] CHANGELOG section from conventional commits.
# Appends to CHANGELOG.md (or prints to stdout with --dry-run).
#
# Usage:
#   ./scripts/draft-changelog.sh            # Append draft to CHANGELOG.md
#   ./scripts/draft-changelog.sh --dry-run  # Print draft without modifying file

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CHANGELOG="$REPO_DIR/CHANGELOG.md"
DRY_RUN=false

for arg in "$@"; do
    [[ "$arg" == "--dry-run" ]] && DRY_RUN=true
done

# ── Find range ─────────────────────────────────────────────
LAST_TAG=$(git -C "$REPO_DIR" describe --tags --abbrev=0 2>/dev/null || true)
if [[ -n "$LAST_TAG" ]]; then
    RANGE="$LAST_TAG..HEAD"
    echo "  Drafting changes since $LAST_TAG..."
else
    # No tags yet — cap at 200 commits to keep the draft manageable
    RANGE="HEAD"
    LOG_EXTRA="--max-count=200"
    echo "  No tags found — drafting last 200 commits (first release)..."
fi

# ── Collect commits by conventional prefix ─────────────────
added=()
fixed=()
changed=()

while IFS= read -r line; do
    hash="${line%% *}"
    msg="${line#* }"
    # Strip scope (e.g. "feat(telegram): ..." → "feat: ...")
    prefix="${msg%%:*}"
    body="${msg#*: }"
    prefix_bare="${prefix%%(*}"

    case "$prefix_bare" in
        feat)      added+=("- $body") ;;
        fix)       fixed+=("- $body") ;;
        refactor)  changed+=("- $body (refactor)") ;;
        docs)      ;;  # omit docs commits — rarely user-facing
        chore)     ;;  # omit chore
        test)      ;;  # omit test
        *)
            # Non-conventional commit — include in Changed if not a merge
            if [[ "$msg" != Merge* ]]; then
                changed+=("- $msg")
            fi
            ;;
    esac
done < <(git -C "$REPO_DIR" log "$RANGE" ${LOG_EXTRA:-} --oneline --no-merges)

# ── Build draft text ───────────────────────────────────────
draft="## [Unreleased]
<!-- Draft generated $(date -u +"%Y-%m-%dT%H:%M:%SZ") from $(git -C "$REPO_DIR" rev-parse --short HEAD) -->
<!-- Curate before releasing: remove noise, group related items, rewrite for users -->"

if [[ ${#added[@]} -gt 0 ]]; then
    draft+="

### Added"
    for item in "${added[@]}"; do
        draft+="
$item"
    done
fi

if [[ ${#fixed[@]} -gt 0 ]]; then
    draft+="

### Fixed"
    for item in "${fixed[@]}"; do
        draft+="
$item"
    done
fi

if [[ ${#changed[@]} -gt 0 ]]; then
    draft+="

### Changed"
    for item in "${changed[@]}"; do
        draft+="
$item"
    done
fi

if [[ ${#added[@]} -eq 0 && ${#fixed[@]} -eq 0 && ${#changed[@]} -eq 0 ]]; then
    draft+="

<!-- No categorizable commits found in range $RANGE -->"
fi

# ── Output ─────────────────────────────────────────────────
# Write draft to a temp file to avoid awk -v backslash interpretation
# (commit messages can contain \n, \t, \" which awk -v mangles)
DRAFT_FILE="$(mktemp)"
printf '%s\n' "$draft" > "$DRAFT_FILE"
trap 'rm -f "$DRAFT_FILE"' EXIT

if $DRY_RUN; then
    echo ""
    cat "$DRAFT_FILE"
    echo ""
    exit 0
fi

if [[ ! -f "$CHANGELOG" ]]; then
    echo "  ERROR: $CHANGELOG not found. Run from the repo root." >&2
    exit 1
fi

# Check for existing [Unreleased] section — don't add a duplicate
if grep -q "^## \[Unreleased\]" "$CHANGELOG"; then
    echo "  WARNING: CHANGELOG.md already has an [Unreleased] section."
    echo "  Edit it manually or remove it first, then re-run."
    exit 1
fi

# Insert the draft after the first "---" separator line
awk -v draftfile="$DRAFT_FILE" '
    /^---$/ && !inserted {
        print
        print ""
        while ((getline line < draftfile) > 0) print line
        inserted = 1
        next
    }
    { print }
' "$CHANGELOG" > "${CHANGELOG}.tmp" && mv "${CHANGELOG}.tmp" "$CHANGELOG"

echo "  Draft appended to CHANGELOG.md"
echo "  Review and curate before releasing."
