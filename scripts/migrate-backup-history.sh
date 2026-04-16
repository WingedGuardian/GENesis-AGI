#!/usr/bin/env bash
# Strip pre-encryption plaintext payloads from genesis-backups git history.
#
# After PR#48 (2026-04-16), backup.sh encrypts SQLite dumps, transcripts,
# memory files, and Qdrant snapshots before pushing. But git history still
# contains older commits with those same payloads in plaintext. This script
# removes those plaintext files from ALL history, leaving encrypted (.gpg)
# versions and everything else intact.
#
# REMOVES from history:
#   data/genesis.sql         — plaintext SQLite dump
#   data/genesis.db          — raw DB fallback
#   transcripts/*.jsonl      — plaintext CC transcripts
#   memory/*.md, **/*.md     — plaintext auto-memory
#   memory/*.json, **/*.json — plaintext auto-memory metadata
#
# PRESERVES:
#   *.gpg                    — encrypted payloads
#   data/qdrant/             — Qdrant snapshots (now encrypted too)
#   config_overrides/        — local config overlays
#   cc-memory/               — in-repo CC memory backup
#   secrets/                 — already encrypted pre-PR#48
#
# REQUIREMENTS:
#   - git-filter-repo (pip install git-filter-repo, or distro package)
#   - Clean working tree in the target backup repo
#   - You must manually force-push afterward; this script does NOT push
#
# Usage:
#   scripts/migrate-backup-history.sh [--backup-dir <path>] [--dry-run] [--yes]
set -euo pipefail

# ── Args ─────────────────────────────────────────────────────────────
BACKUP_DIR="${HOME:-}/backups/genesis-backups"
DRY_RUN=false
AUTO_YES=false

while [ $# -gt 0 ]; do
    case "$1" in
        --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
        --dry-run)    DRY_RUN=true; shift ;;
        --yes)        AUTO_YES=true; shift ;;
        -h|--help)
            grep -E '^#( |$)' "$0" | sed 's/^# //; s/^#//'
            exit 0 ;;
        *)
            echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

log() { echo "[migrate-backup-history] $*"; }
die() { log "FATAL: $*"; exit 1; }

# ── Preflight checks ────────────────────────────────────────────────
if ! command -v git-filter-repo >/dev/null 2>&1; then
    die "git-filter-repo not found. Install it:
  pip install git-filter-repo
  # or: apt install git-filter-repo  (Ubuntu 22.04+)
  # or: brew install git-filter-repo (macOS)"
fi

[ -d "$BACKUP_DIR/.git" ] || die "Not a git repo: $BACKUP_DIR"

cd "$BACKUP_DIR"

if [ -n "$(git status --porcelain)" ]; then
    die "Working tree is dirty. Commit or stash changes first."
fi

# ── Check if there's anything to strip ──────────────────────────────
_has_plaintext=false
for pattern in data/genesis.sql data/genesis.db; do
    if git log --all --oneline -- "$pattern" 2>/dev/null | head -1 | grep -q .; then
        _has_plaintext=true
        break
    fi
done
if ! $_has_plaintext; then
    # Check glob patterns
    for glob_pat in 'transcripts/*.jsonl' 'memory/*.md' 'memory/**/*.md' 'memory/**/*.json'; do
        if git log --all --oneline --diff-filter=A -- "$glob_pat" 2>/dev/null | head -1 | grep -q .; then
            _has_plaintext=true
            break
        fi
    done
fi

if ! $_has_plaintext; then
    log "No pre-encryption plaintext payloads found in history. Nothing to do."
    exit 0
fi

# ── Show plan ────────────────────────────────────────────────────────
SIZE_BEFORE=$(du -sh .git | cut -f1)
COMMIT_COUNT=$(git rev-list --all --count)

log ""
log "Backup repo: $BACKUP_DIR"
log "Commits: $COMMIT_COUNT"
log ".git size before: $SIZE_BEFORE"
log ""
log "Paths to REMOVE from ALL history:"
log "  data/genesis.sql, data/genesis.db"
log "  transcripts/*.jsonl"
log "  memory/*.md, memory/**/*.md, memory/**/*.json"
log ""
log "Everything else is PRESERVED (*.gpg, data/qdrant/, config_overrides/, cc-memory/, secrets/)"
log ""

if $DRY_RUN; then
    log "[dry-run] Would rewrite $COMMIT_COUNT commits. No changes made."
    exit 0
fi

# ── Confirm ──────────────────────────────────────────────────────────
if ! $AUTO_YES; then
    read -r -p "Rewrite history in $BACKUP_DIR? This is irreversible locally. [y/N] " reply
    if ! [[ "$reply" =~ ^[yY]([eE][sS])?$ ]]; then
        log "Aborted."
        exit 0
    fi
fi

# ── Run filter-repo ─────────────────────────────────────────────────
log "Rewriting history..."
git filter-repo \
    --invert-paths \
    --path data/genesis.sql \
    --path data/genesis.db \
    --path-glob 'transcripts/*.jsonl' \
    --path-glob 'memory/*.md' \
    --path-glob 'memory/**/*.md' \
    --path-glob 'memory/**/*.json' \
    --force

# ── GC ───────────────────────────────────────────────────────────────
log "Running garbage collection..."
git gc --aggressive --prune=now 2>/dev/null

SIZE_AFTER=$(du -sh .git | cut -f1)

log ""
log "Done."
log ".git size: $SIZE_BEFORE → $SIZE_AFTER"
log ""
log "To propagate, run:"
log "  cd $BACKUP_DIR"
log "  git push --force --all origin"
log "  git push --force --tags origin"
log ""
log "WARNING: force-push rewrites remote history. Any other clones of"
log "this repo must re-clone after the push."
