#!/bin/bash
# Backup Claude Code project memory to the repo's data directory.
# Included in the 6h backup cron (data/cc-memory-backup/ is gitignored).
#
# Usage: ./scripts/backup_cc_memory.sh [--genesis-root /path/to/genesis]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENESIS_ROOT="${1:-$(cd "$SCRIPT_DIR/.." && pwd)}"

# Compute CC project directory name: /path/to/genesis -> -path-to-genesis
CC_PROJECT_DIR_NAME="$(echo "$GENESIS_ROOT" | sed 's|/|-|g')"
CC_MEMORY_DIR="$HOME/.claude/projects/${CC_PROJECT_DIR_NAME}/memory"
BACKUP_DIR="$GENESIS_ROOT/data/cc-memory-backup"

if [[ ! -d "$CC_MEMORY_DIR" ]]; then
    echo "No CC memory directory found at: $CC_MEMORY_DIR"
    echo "Nothing to back up."
    exit 0
fi

mkdir -p "$BACKUP_DIR"

# Copy preserving timestamps. Remove stale files first, then copy fresh.
rm -rf "$BACKUP_DIR"/*
cp -a "$CC_MEMORY_DIR"/. "$BACKUP_DIR/"

FILE_COUNT=$(find "$BACKUP_DIR" -type f | wc -l)
TOTAL_SIZE=$(du -sh "$BACKUP_DIR" | cut -f1)
echo "Backed up $FILE_COUNT files ($TOTAL_SIZE) to $BACKUP_DIR"
