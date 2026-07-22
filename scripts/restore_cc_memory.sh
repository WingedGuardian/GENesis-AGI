#!/bin/bash
# Restore Claude Code project memory from the repo's backup.
# Computes the correct CC project directory for the current machine.
#
# Usage: ./scripts/restore_cc_memory.sh [--genesis-root /path/to/genesis]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENESIS_ROOT="${1:-$(cd "$SCRIPT_DIR/.." && pwd)}"

BACKUP_DIR="$GENESIS_ROOT/data/cc-memory-backup"

if [[ ! -d "$BACKUP_DIR" ]]; then
    echo "No backup found at: $BACKUP_DIR"
    echo "Nothing to restore."
    exit 0
fi

# Compute CC project directory name: /path/to/genesis -> -path-to-genesis
CC_PROJECT_DIR_NAME="$(echo "$GENESIS_ROOT" | sed 's|/|-|g')"
CC_MEMORY_DIR="$HOME/.claude/projects/${CC_PROJECT_DIR_NAME}/memory"

echo "Genesis root:     $GENESIS_ROOT"
echo "CC project dir:   $CC_PROJECT_DIR_NAME"
echo "Restore target:   $CC_MEMORY_DIR"
echo "Backup source:    $BACKUP_DIR"

mkdir -p "$CC_MEMORY_DIR"

# Copy files the machine doesn't already have, NEVER overwriting existing ones
# (preserve work done on this machine). The old `cp -an … || cp -a …` was a
# data-loss trap: coreutils 9.1+ makes `cp -an` return NON-ZERO when it SKIPS an
# existing file (skip-counts-as-failure), so on a normal run the `|| cp -a`
# fallback fired and CLOBBERED the newer local files `-n` exists to protect.
# Prefer rsync --ignore-existing (stable, version-independent skip semantics);
# fall back to `cp -an` best-effort (its skip-as-failure exit is swallowed — a
# skipped existing file is the intended outcome, not an error to "recover" from
# by overwriting).
# The caller (restore.sh §5) keys _CCMEM_RESTORED on this script's exit status,
# so the copy must be no-clobber AND propagate a REAL failure (unreadable
# source, permission, full disk) as a non-zero exit — while NOT failing merely
# because it skipped an existing file.
if command -v rsync >/dev/null 2>&1; then
    # rsync: --ignore-existing is stable no-clobber with proper exit codes.
    rsync -a --ignore-existing "$BACKUP_DIR"/ "$CC_MEMORY_DIR/"
elif cp --help 2>/dev/null | grep -q -- '--update'; then
    # coreutils 9.3+: `--update=none` is the stable no-clobber flag (the one
    # `cp -n`'s own deprecation warning points to). A skip exits 0, a real error
    # exits non-zero — so let the exit status propagate under `set -e`. (cp -n on
    # 9.1-9.2 counted a skip as failure and 9.3+ warns about it — both avoided.)
    cp -a --update=none "$BACKUP_DIR"/. "$CC_MEMORY_DIR/"
else
    # Pre-9.3 coreutils without --update=none: cp -an is no-clobber but its exit
    # code can't distinguish a skip from a real error, so this path is
    # best-effort only (the two paths above carry clean error propagation).
    cp -an "$BACKUP_DIR"/. "$CC_MEMORY_DIR/" 2>/dev/null || true
fi

FILE_COUNT=$(find "$CC_MEMORY_DIR" -type f | wc -l)
TOTAL_SIZE=$(du -sh "$CC_MEMORY_DIR" | cut -f1)
echo "Restored to $FILE_COUNT files ($TOTAL_SIZE) at $CC_MEMORY_DIR"
