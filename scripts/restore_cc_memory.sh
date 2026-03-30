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

# Copy without overwriting newer files (preserve work done on this machine)
# Using cp -an (no-clobber) to avoid overwriting existing files
cp -an "$BACKUP_DIR"/. "$CC_MEMORY_DIR/" 2>/dev/null || \
    cp -a "$BACKUP_DIR"/. "$CC_MEMORY_DIR/"

FILE_COUNT=$(find "$CC_MEMORY_DIR" -type f | wc -l)
TOTAL_SIZE=$(du -sh "$CC_MEMORY_DIR" | cut -f1)
echo "Restored to $FILE_COUNT files ($TOTAL_SIZE) at $CC_MEMORY_DIR"
