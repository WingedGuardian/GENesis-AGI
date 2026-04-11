#!/usr/bin/env bash
# Phase 6 — idempotent hook sync tool.
#
# Brings $GIT_COMMON_DIR/hooks into sync with scripts/hooks/ without clobbering
# user-modified hooks. Designed to run on every Genesis session start so
# community users who `git pull` without re-running bootstrap.sh automatically
# pick up new or updated hooks.
#
# Idempotent: running twice is a no-op the second time.
# Safe: if a destination hook exists AND differs from the source AND does NOT
#       match any known prior version (tracked via content hash markers),
#       it's assumed to be user-modified and is LEFT ALONE with a warning.
#
# Usage:
#     scripts/hooks/sync-hooks.sh [--quiet]
#
# Exit codes:
#     0 — all hooks in sync (or newly synced)
#     1 — could not locate git hooks dir (non-fatal for callers; hooks degrade)
#     2 — one or more user-modified hooks skipped (caller may want to warn)

set -u
set -o pipefail

QUIET=0
if [[ "${1:-}" == "--quiet" ]]; then
    QUIET=1
fi

_log() {
    [[ $QUIET -eq 1 ]] && return 0
    printf '[sync-hooks] %s\n' "$*"
}

_warn() {
    printf '[sync-hooks] WARNING: %s\n' "$*" >&2
}

# Locate the source (this script's sibling hooks) and destination.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOKS_SRC="$SCRIPT_DIR"

# Find .git/hooks for this repo, handling worktrees (where .git is a file,
# not a dir). git rev-parse --git-common-dir points at the shared dir.
cd "$REPO_ROOT" || exit 1
GIT_COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null || echo "")
if [[ -z "$GIT_COMMON_DIR" ]]; then
    _warn "not a git repo, skipping"
    exit 1
fi
# git-common-dir can be relative; resolve to absolute.
if [[ "$GIT_COMMON_DIR" != /* ]]; then
    GIT_COMMON_DIR="$REPO_ROOT/$GIT_COMMON_DIR"
fi
HOOKS_DST="$GIT_COMMON_DIR/hooks"

if [[ ! -d "$HOOKS_DST" ]]; then
    # git init creates the hooks dir, so absence is weird — create it.
    mkdir -p "$HOOKS_DST" 2>/dev/null || { _warn "cannot create $HOOKS_DST"; exit 1; }
fi

# Hooks to sync. Executable hook scripts AND their colocated helpers.
# Extend this list as Phase 6 adds more hooks.
HOOKS_TO_SYNC=(
    "post-commit"
    "pre-commit"
    "pre-push"
)
# Colocated helpers (called by the hooks via $(dirname $0)/helper.py).
HELPERS_TO_SYNC=(
    "emit_bugfix_audit.py"
)

MODIFIED_FOUND=0
SYNCED=0
SKIPPED=0

_sha256_of() {
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
}

_sync_one() {
    local name="$1"
    local src="$HOOKS_SRC/$name"
    local dst="$HOOKS_DST/$name"

    if [[ ! -f "$src" ]]; then
        # Source doesn't exist — nothing to sync. Not an error (some hooks are
        # listed but may not exist yet in pre-Phase6 commits).
        return 0
    fi

    local src_hash dst_hash
    src_hash=$(_sha256_of "$src")

    if [[ ! -f "$dst" ]]; then
        # Fresh install — just copy.
        cp "$src" "$dst" && chmod +x "$dst" || { _warn "cp failed for $name"; return 1; }
        _log "installed: $name"
        SYNCED=$((SYNCED + 1))
        return 0
    fi

    dst_hash=$(_sha256_of "$dst")

    if [[ "$src_hash" == "$dst_hash" ]]; then
        # Already in sync. No-op (idempotent).
        return 0
    fi

    # Destination exists and differs from source. Decide: safe overwrite or
    # user-modified.
    #
    # We track "known prior versions" via the file .genesis-hook-versions in
    # the repo, which lists sha256 hashes of all versions Genesis has ever
    # shipped. If the destination hash matches a known prior, it's just stale
    # — safe to overwrite. Otherwise it's user-modified — skip with warning.
    local versions_file="$REPO_ROOT/.genesis-hook-versions"
    local is_known_prior=0
    if [[ -f "$versions_file" ]]; then
        if grep -q "^$name:$dst_hash$" "$versions_file" 2>/dev/null; then
            is_known_prior=1
        fi
    fi

    if [[ $is_known_prior -eq 1 ]]; then
        cp "$src" "$dst" && chmod +x "$dst" || { _warn "cp failed for $name"; return 1; }
        _log "updated: $name (was stale prior version)"
        SYNCED=$((SYNCED + 1))
    else
        _warn "skipping $name: destination differs from source and is not a known prior version (user-modified?). Left alone."
        _warn "  src: $src"
        _warn "  dst: $dst"
        SKIPPED=$((SKIPPED + 1))
        MODIFIED_FOUND=1
    fi
}

_sync_helper() {
    # Helpers are .py files next to the hooks. They are NEVER user-modified by
    # convention, so they always sync. No version tracking needed.
    local name="$1"
    local src="$HOOKS_SRC/$name"
    local dst="$HOOKS_DST/$name"

    if [[ ! -f "$src" ]]; then
        return 0
    fi

    local src_hash dst_hash
    src_hash=$(_sha256_of "$src")

    if [[ ! -f "$dst" ]]; then
        cp "$src" "$dst" && chmod +x "$dst" || { _warn "cp failed for helper $name"; return 1; }
        _log "installed helper: $name"
        SYNCED=$((SYNCED + 1))
        return 0
    fi

    dst_hash=$(_sha256_of "$dst")
    if [[ "$src_hash" == "$dst_hash" ]]; then
        return 0
    fi

    # Helper drift — always overwrite.
    cp "$src" "$dst" && chmod +x "$dst" || { _warn "cp failed for helper $name"; return 1; }
    _log "updated helper: $name"
    SYNCED=$((SYNCED + 1))
}

for h in "${HOOKS_TO_SYNC[@]}"; do
    _sync_one "$h"
done

for h in "${HELPERS_TO_SYNC[@]}"; do
    _sync_helper "$h"
done

if [[ $SYNCED -gt 0 ]]; then
    _log "synced $SYNCED file(s) to $HOOKS_DST"
fi

if [[ $MODIFIED_FOUND -eq 1 ]]; then
    exit 2
fi

exit 0
