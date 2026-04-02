#!/usr/bin/env bash
# apply_gstack_patches.sh — Reapply Genesis customizations to gstack after updates.
#
# Usage:
#   scripts/apply_gstack_patches.sh          # Apply all patches
#   scripts/apply_gstack_patches.sh --check  # Check if patches need reapplying (no changes)
#
# GStack updates (git reset --hard origin/main) wipe all local modifications.
# This script restores Genesis-specific patches from config/gstack-patches/.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PATCHES_DIR="$PROJECT_DIR/config/gstack-patches"
GSTACK_DIR="$HOME/.claude/skills/gstack"
HASH_FILE="$PATCHES_DIR/.last-applied-hash"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err()  { echo -e "${RED}[ERR]${NC} $1"; }

# --- Preflight ---

if [[ ! -d "$GSTACK_DIR" ]]; then
    log_err "GStack not found at $GSTACK_DIR"
    exit 1
fi

if [[ ! -d "$GSTACK_DIR/.git" ]]; then
    log_err "GStack is not a git repo — cannot track state"
    exit 1
fi

if [[ ! -d "$PATCHES_DIR" ]]; then
    log_err "Patches directory not found at $PATCHES_DIR"
    exit 1
fi

# Get current gstack HEAD
CURRENT_HASH=$(cd "$GSTACK_DIR" && git rev-parse HEAD)
CURRENT_SHORT=$(cd "$GSTACK_DIR" && git log --oneline -1)

# --- Check mode ---

if [[ "${1:-}" == "--check" ]]; then
    if [[ -f "$HASH_FILE" ]]; then
        LAST_HASH=$(cat "$HASH_FILE")
        if [[ "$CURRENT_HASH" == "$LAST_HASH" ]]; then
            echo "Patches are up to date (gstack at $CURRENT_HASH)"
            exit 0
        else
            log_warn "GStack updated since last patch application"
            echo "  Last patched: $LAST_HASH"
            echo "  Current HEAD: $CURRENT_HASH"
            echo "  Run: scripts/apply_gstack_patches.sh"
            exit 1
        fi
    else
        log_warn "No patch history found — patches may not be applied"
        exit 1
    fi
fi

# --- Apply patches ---

echo "Applying Genesis patches to gstack..."
echo "  GStack: $GSTACK_DIR"
echo "  HEAD:   $CURRENT_SHORT"
echo ""

APPLIED=0
FAILED=0

# Safe increment that won't trigger set -e on 0->1
inc_applied() { APPLIED=$((APPLIED + 1)); }
inc_failed()  { FAILED=$((FAILED + 1)); }

# 1. Full file overlays
apply_overlay() {
    local src="$1"
    local dest="$2"
    local name="$3"

    if [[ ! -f "$src" ]]; then
        log_warn "Overlay source not found: $src (skipping $name)"
        return
    fi

    local dest_dir
    dest_dir=$(dirname "$dest")
    if [[ ! -d "$dest_dir" ]]; then
        log_warn "Target directory missing: $dest_dir (skipping $name)"
        inc_failed
        return
    fi

    cp "$src" "$dest"
    log_ok "$name -> $(basename "$dest")"
    inc_applied
}

apply_overlay "$PATCHES_DIR/codex-SKILL.md.tmpl" "$GSTACK_DIR/codex/SKILL.md.tmpl" "Codex 2.0 template"
apply_overlay "$PATCHES_DIR/codex-SKILL.md"      "$GSTACK_DIR/codex/SKILL.md"      "Codex 2.0 skill"
apply_overlay "$PATCHES_DIR/review-checklist.md"  "$GSTACK_DIR/review/checklist.md" "Review checklist"

# 2. Safety frontmatter patch (ship + land-and-deploy)
apply_safety_frontmatter() {
    local skill_file="$1"
    local skill_name="$2"

    if [[ ! -f "$skill_file" ]]; then
        log_warn "Skill not found: $skill_file (skipping $skill_name safety)"
        return
    fi

    if grep -q "disable-model-invocation:" "$skill_file"; then
        log_ok "$skill_name already has disable-model-invocation (no change)"
        return
    fi

    # Insert disable-model-invocation: true after the name: line in frontmatter
    sed -i '/^name: '"$skill_name"'/a disable-model-invocation: true' "$skill_file"

    if grep -q "disable-model-invocation: true" "$skill_file"; then
        log_ok "$skill_name: added disable-model-invocation: true"
        inc_applied
    else
        log_err "$skill_name: failed to add safety frontmatter"
        inc_failed
    fi
}

apply_safety_frontmatter "$GSTACK_DIR/ship/SKILL.md" "ship"
apply_safety_frontmatter "$GSTACK_DIR/land-and-deploy/SKILL.md" "land-and-deploy"

# 3. Description trim patches (applied via sed)
if [[ -f "$PATCHES_DIR/descriptions.sh" ]]; then
    echo ""
    echo "Applying description trims..."
    if bash "$PATCHES_DIR/descriptions.sh" "$GSTACK_DIR"; then
        log_ok "Description trims applied"
        inc_applied
    else
        log_err "Description trims failed"
        inc_failed
    fi
fi

# --- Record state ---

echo "$CURRENT_HASH" > "$HASH_FILE"

echo ""
echo "=== Summary ==="
echo "  Applied: $APPLIED"
echo "  Failed:  $FAILED"
echo "  GStack HEAD: $CURRENT_HASH"

if [[ $FAILED -gt 0 ]]; then
    log_warn "Some patches failed — check output above"
    exit 1
fi

log_ok "All patches applied successfully"
