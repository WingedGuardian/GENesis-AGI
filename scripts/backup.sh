#!/usr/bin/env bash
# Genesis automated backup — runs every 6h via cron.
# Backs up: SQLite DB, Qdrant snapshots, CC transcripts, auto-memory, secrets.
#
# Environment variables (all optional):
#   GENESIS_BACKUP_REPO  — Git URL for backup repo (auto-detected from existing clone)
#   GENESIS_DIR          — Genesis repo root (default: ~/genesis)
#   AZ_ROOT              — Agent Zero root (default: ~/agent-zero)
#   QDRANT_URL           — Qdrant server URL (default: http://localhost:6333)
#   SECRETS_PATH         — Path to secrets.env (default: $GENESIS_DIR/secrets.env)
set -euo pipefail

GENESIS_DIR="${GENESIS_DIR:-$HOME/genesis}"
AZ_DIR="${AZ_ROOT:-$HOME/agent-zero}"
BACKUP_DIR="$HOME/backups/genesis-backups"
# Derive CC project dir from genesis dir path (CC convention: / → -)
_CC_PROJECT_ID=$(echo "$GENESIS_DIR" | tr '/' '-')
MEMORY_DIR="$HOME/.claude/projects/${_CC_PROJECT_ID}/memory"
TRANSCRIPT_DIR="$HOME/.claude/projects/${_CC_PROJECT_ID}"
SECRETS_FILE="${SECRETS_PATH:-$GENESIS_DIR/secrets.env}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
LOG_PREFIX="[genesis-backup]"

log() { echo "$LOG_PREFIX $(date -Iseconds) $*"; }

die() { log "FATAL: $*"; exit 1; }

# --- Clone or pull backup repo ---
if [ ! -d "$BACKUP_DIR/.git" ]; then
    # Determine backup repo URL: env var → auto-detect from existing clone → fail
    BACKUP_REPO="${GENESIS_BACKUP_REPO:-}"
    if [ -z "$BACKUP_REPO" ]; then
        log "GENESIS_BACKUP_REPO not set. Set it in secrets.env or environment."
        log "  Example: GENESIS_BACKUP_REPO=https://github.com/YOUR_USER/genesis-backups.git"
        die "Cannot clone backup repo without GENESIS_BACKUP_REPO"
    fi
    log "Cloning backup repo..."
    mkdir -p "$(dirname "$BACKUP_DIR")"
    git clone "$BACKUP_REPO" "$BACKUP_DIR"
fi

cd "$BACKUP_DIR"

# Ensure git identity is configured (per-repo, not global)
git config user.name "Genesis Backup" 2>/dev/null || true
git config user.email "backup@genesis.local" 2>/dev/null || true

git pull --rebase --quiet 2>/dev/null || log "WARNING: git pull failed, continuing with local state"

# --- 1. SQLite dump (portable SQL text, diffs well in git) ---
log "Backing up SQLite database..."
mkdir -p data
DB_FILE="$GENESIS_DIR/data/genesis.db"
if [ -f "$DB_FILE" ]; then
    sqlite3 "$DB_FILE" .dump > data/genesis.sql 2>/dev/null || {
        log "WARNING: sqlite3 dump failed, copying raw DB"
        cp "$DB_FILE" data/genesis.db
    }
    log "SQLite: $(wc -l < data/genesis.sql) lines"
else
    log "WARNING: genesis.db not found at $DB_FILE"
fi

# --- 2. Qdrant snapshots ---
log "Backing up Qdrant collections..."
mkdir -p data/qdrant
for collection in episodic_memory knowledge_base; do
    # Create snapshot via Qdrant API
    snapshot_resp=$(curl -sf -X POST "$QDRANT_URL/collections/$collection/snapshots" 2>/dev/null) || {
        log "WARNING: Qdrant snapshot failed for $collection (collection may not exist)"
        continue
    }
    snapshot_name=$(echo "$snapshot_resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['name'])" 2>/dev/null) || {
        log "WARNING: Could not parse snapshot response for $collection"
        continue
    }
    # Download snapshot
    curl -sf "$QDRANT_URL/collections/$collection/snapshots/$snapshot_name" \
        -o "data/qdrant/${collection}.snapshot" 2>/dev/null || {
        log "WARNING: Could not download snapshot for $collection"
        continue
    }
    log "Qdrant: $collection snapshot saved ($(du -sh "data/qdrant/${collection}.snapshot" | cut -f1))"

    # Clean up snapshot from Qdrant server
    curl -sf -X DELETE "$QDRANT_URL/collections/$collection/snapshots/$snapshot_name" >/dev/null 2>&1 || true
done

# --- 3. CC session transcripts ---
log "Backing up CC transcripts..."
mkdir -p transcripts
if [ -d "$TRANSCRIPT_DIR" ]; then
    # Copy only .jsonl files from the top-level directory (not subdirs like memory/)
    find "$TRANSCRIPT_DIR" -maxdepth 1 -name '*.jsonl' -exec cp -u {} transcripts/ \; 2>/dev/null || {
        log "WARNING: transcript copy failed"
    }
    transcript_count=$(find transcripts -name '*.jsonl' 2>/dev/null | wc -l)
    log "Transcripts: $transcript_count files"
else
    log "WARNING: transcript directory not found"
fi

# --- 4. Auto-memory files ---
log "Backing up auto-memory..."
mkdir -p memory
if [ -d "$MEMORY_DIR" ]; then
    cp -ru "$MEMORY_DIR"/* memory/ 2>/dev/null || {
        log "WARNING: memory copy failed"
    }
    memory_count=$(find memory -type f 2>/dev/null | wc -l)
    log "Memory: $memory_count files"
else
    log "WARNING: memory directory not found"
fi

# --- 5. CC memory local backup (in-repo, for portability) ---
log "Backing up CC memory to genesis repo..."
if [ -x "$GENESIS_DIR/scripts/backup_cc_memory.sh" ]; then
    bash "$GENESIS_DIR/scripts/backup_cc_memory.sh" "$GENESIS_DIR" || \
        log "WARNING: CC memory backup failed"
else
    log "WARNING: backup_cc_memory.sh not found"
fi

# --- 6. Secrets (encrypted with GPG symmetric) ---
log "Backing up secrets (encrypted)..."
mkdir -p secrets
if [ -f "$SECRETS_FILE" ]; then
    # Use a passphrase from environment or a fixed key file
    BACKUP_PASSPHRASE="${GENESIS_BACKUP_PASSPHRASE:-}"
    if [ -n "$BACKUP_PASSPHRASE" ]; then
        echo "$BACKUP_PASSPHRASE" | gpg --batch --yes --passphrase-fd 0 \
            --symmetric --cipher-algo AES256 \
            -o secrets/secrets.env.gpg "$SECRETS_FILE" 2>/dev/null
        log "Secrets: encrypted with GPG"
    else
        log "WARNING: GENESIS_BACKUP_PASSPHRASE not set, skipping secrets backup"
    fi
else
    log "WARNING: secrets file not found at $SECRETS_FILE"
fi

# --- Commit and push ---
log "Committing backup..."
git add -A
if git diff --cached --quiet; then
    log "No changes since last backup"
else
    git commit -m "backup: $(date -Iseconds)" --quiet
    git push --quiet || {
        log "WARNING: git push failed — backup committed locally only"
    }
    log "Backup committed and pushed"
fi

log "Backup complete"
