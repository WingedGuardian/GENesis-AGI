#!/usr/bin/env bash
# Genesis automated backup — runs every 6h via cron.
# Writes your Genesis state to <your-gh-user>/genesis-backups.
# All PII-bearing payloads are GPG-encrypted with GENESIS_BACKUP_PASSPHRASE.
#
# Backs up: SQLite DB, Qdrant snapshots, CC transcripts, auto-memory,
# local config overlays, secrets.
#
# Restore via scripts/restore.sh or `python -m genesis restore`.
#
# Environment variables (all optional unless noted):
#   GENESIS_BACKUP_REPO        — Git URL for backup repo (auto-detected from existing clone)
#   GENESIS_BACKUP_PASSPHRASE  — GPG passphrase (REQUIRED for secrets + encrypted payloads)
#   GENESIS_DIR                — Genesis repo root (default: ~/genesis)
#   QDRANT_URL                 — Qdrant server URL (default: http://localhost:6333)
#   SECRETS_PATH               — Path to secrets.env (default: $GENESIS_DIR/secrets.env)
set -euo pipefail

# ── Status tracking ──────────────────────────────────────────────────
_STATUS_FILE="$HOME/.genesis/backup_status.json"
_STARTED_AT=$(date +%s)
_SQLITE_LINES=0
_QDRANT_COUNT=0
_TRANSCRIPT_COUNT=0
_MEMORY_COUNT=0
_SECRETS_OK=false
_SUCCESS=false

_write_status() {
    local _ended_at
    _ended_at=$(date +%s)
    local _duration=$(( _ended_at - _STARTED_AT ))
    mkdir -p "$(dirname "$_STATUS_FILE")"
    cat > "$_STATUS_FILE" <<STATUSEOF
{"timestamp":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","success":$_SUCCESS,"sqlite_lines":$_SQLITE_LINES,"qdrant_collections":$_QDRANT_COUNT,"transcript_files":$_TRANSCRIPT_COUNT,"memory_files":$_MEMORY_COUNT,"secrets_encrypted":$_SECRETS_OK,"duration_s":$_duration}
STATUSEOF
}
trap '_write_status' EXIT

GENESIS_DIR="${GENESIS_DIR:-$HOME/genesis}"
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

# ── Encryption helpers ───────────────────────────────────────────────
# All PII-bearing payloads (SQLite dump, transcripts, memory) use the
# same GPG symmetric passphrase as secrets. If the passphrase is unset,
# encrypted sections are SKIPPED rather than falling back to plaintext
# (the memory system is designed to hold credentials — plaintext leak
# to a private repo is not acceptable).
_BACKUP_PASSPHRASE="${GENESIS_BACKUP_PASSPHRASE:-}"
_ENCRYPT_READY=false
if [ -n "$_BACKUP_PASSPHRASE" ]; then
    _ENCRYPT_READY=true
fi

# encrypt_stdin <output_path> — read plaintext from stdin, write <output_path>.
encrypt_stdin() {
    local out="$1"
    printf '%s' "$_BACKUP_PASSPHRASE" | gpg --batch --yes --passphrase-fd 0 \
        --symmetric --cipher-algo AES256 -o "$out" 2>/dev/null
}

# encrypt_file <src> <dst> — encrypt file contents to <dst> (e.g. *.gpg).
encrypt_file() {
    local src="$1"
    local dst="$2"
    printf '%s' "$_BACKUP_PASSPHRASE" | gpg --batch --yes --passphrase-fd 0 \
        --symmetric --cipher-algo AES256 -o "$dst" "$src" 2>/dev/null
}

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

# --- 1. SQLite dump (encrypted — may hold memory-stored credentials/PII) ---
log "Backing up SQLite database..."
mkdir -p data
DB_FILE="$GENESIS_DIR/data/genesis.db"
# Purge any pre-encryption plaintext dumps so they don't persist in the
# backup repo alongside the new encrypted form.
rm -f data/genesis.sql data/genesis.db
if [ -f "$DB_FILE" ]; then
    if ! $_ENCRYPT_READY; then
        log "WARNING: GENESIS_BACKUP_PASSPHRASE not set — skipping SQLite backup (refusing plaintext)"
    else
        _SQL_TMP=$(mktemp)
        if sqlite3 "$DB_FILE" .dump > "$_SQL_TMP" 2>/dev/null; then
            _SQLITE_LINES=$(wc -l < "$_SQL_TMP")
            if encrypt_file "$_SQL_TMP" data/genesis.sql.gpg; then
                log "SQLite: $_SQLITE_LINES lines (encrypted)"
            else
                log "WARNING: SQLite encryption failed"
                _SQLITE_LINES=0
            fi
        else
            log "WARNING: sqlite3 dump failed"
        fi
        rm -f "$_SQL_TMP"
    fi
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
    _QDRANT_COUNT=$(( _QDRANT_COUNT + 1 ))
    log "Qdrant: $collection snapshot saved ($(du -sh "data/qdrant/${collection}.snapshot" | cut -f1))"

    # Clean up snapshot from Qdrant server
    curl -sf -X DELETE "$QDRANT_URL/collections/$collection/snapshots/$snapshot_name" >/dev/null 2>&1 || true
done

# --- 3. CC session transcripts (encrypted — contain conversation PII) ---
log "Backing up CC transcripts..."
mkdir -p transcripts
# Purge any pre-encryption plaintext transcripts.
find transcripts -maxdepth 1 -name '*.jsonl' -type f -delete 2>/dev/null || true
if [ -d "$TRANSCRIPT_DIR" ]; then
    if ! $_ENCRYPT_READY; then
        log "WARNING: GENESIS_BACKUP_PASSPHRASE not set — skipping transcripts (refusing plaintext)"
    else
        # Encrypt each jsonl to transcripts/<name>.jsonl.gpg. Skip re-encryption
        # when the encrypted copy is newer than the source (mirrors cp -u).
        while IFS= read -r -d '' src; do
            name=$(basename "$src")
            dst="transcripts/${name}.gpg"
            if [ -f "$dst" ] && [ "$dst" -nt "$src" ]; then
                continue
            fi
            encrypt_file "$src" "$dst" || log "WARNING: failed to encrypt $name"
        done < <(find "$TRANSCRIPT_DIR" -maxdepth 1 -name '*.jsonl' -type f -print0)
        _TRANSCRIPT_COUNT=$(find transcripts -maxdepth 1 -name '*.jsonl.gpg' 2>/dev/null | wc -l)
        log "Transcripts: $_TRANSCRIPT_COUNT files (encrypted)"
    fi
else
    log "WARNING: transcript directory not found"
fi

# --- 4. Auto-memory files (encrypted — auto-memory can hold credentials/PII) ---
log "Backing up auto-memory..."
mkdir -p memory
# Purge any pre-encryption plaintext memory files.
find memory -type f ! -name '*.gpg' -delete 2>/dev/null || true
if [ -d "$MEMORY_DIR" ]; then
    if ! $_ENCRYPT_READY; then
        log "WARNING: GENESIS_BACKUP_PASSPHRASE not set — skipping memory (refusing plaintext)"
    else
        # Walk the memory directory, preserve relative structure, encrypt each file.
        # Skip re-encryption when the encrypted copy is newer than the source.
        while IFS= read -r -d '' src; do
            rel="${src#$MEMORY_DIR/}"
            dst="memory/${rel}.gpg"
            mkdir -p "$(dirname "$dst")"
            if [ -f "$dst" ] && [ "$dst" -nt "$src" ]; then
                continue
            fi
            encrypt_file "$src" "$dst" || log "WARNING: failed to encrypt $rel"
        done < <(find "$MEMORY_DIR" -type f -print0)
        _MEMORY_COUNT=$(find memory -type f -name '*.gpg' 2>/dev/null | wc -l)
        log "Memory: $_MEMORY_COUNT files (encrypted)"
    fi
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

# --- 6. Local config overlays (user customizations, gitignored) ---
log "Backing up local config overlays..."
mkdir -p config_overrides
_LOCAL_OVERLAY_COUNT=0
if [ -d "$GENESIS_DIR/config" ]; then
    find "$GENESIS_DIR/config" -maxdepth 1 -name "*.local.yaml" | while IFS= read -r f; do
        cp "$f" config_overrides/ && _LOCAL_OVERLAY_COUNT=$(( _LOCAL_OVERLAY_COUNT + 1 ))
    done
    _LOCAL_OVERLAY_COUNT=$(find config_overrides -name "*.local.yaml" 2>/dev/null | wc -l)
    log "Local overlays: $_LOCAL_OVERLAY_COUNT files"
fi

# --- 7. Secrets (encrypted with GPG symmetric) ---
log "Backing up secrets (encrypted)..."
mkdir -p secrets
if [ -f "$SECRETS_FILE" ]; then
    if $_ENCRYPT_READY; then
        if encrypt_file "$SECRETS_FILE" secrets/secrets.env.gpg; then
            _SECRETS_OK=true
            log "Secrets: encrypted with GPG"
        else
            log "WARNING: secrets encryption failed"
        fi
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

_SUCCESS=true
log "Backup complete"
