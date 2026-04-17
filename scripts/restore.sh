#!/usr/bin/env bash
# Genesis restore — counterpart to scripts/backup.sh.
# Reads your genesis-backups repo and rehydrates Genesis state:
# SQLite, Qdrant collections, CC transcripts, auto-memory, CC memory,
# local config overlays, and secrets.
#
# Usage:
#   scripts/restore.sh [--from <backup-repo-url>] [--dry-run] [--force]
#
# Environment variables (match backup.sh):
#   GENESIS_BACKUP_REPO        — Git URL (used when a fresh clone is needed)
#   GENESIS_BACKUP_PASSPHRASE  — GPG passphrase (REQUIRED to decrypt payloads)
#   GENESIS_DIR                — target Genesis root (default: ~/genesis)
#   QDRANT_URL                 — target Qdrant server (default: http://localhost:6333)
#   SECRETS_PATH               — target secrets file (default: $GENESIS_DIR/secrets.env)
#
# Behavior:
#   - Skips destinations that already exist AND are newer than the backup
#     (avoid clobbering live data). Override with --force.
#   - Reads both encrypted (*.gpg) and legacy plaintext forms for backward
#     compatibility with backups predating the encryption hardening.
#   - Writes ~/.genesis/restore_status.json on every run (success or failure).
set -euo pipefail

# ── Args ─────────────────────────────────────────────────────────────
BACKUP_REPO_OVERRIDE=""
DRY_RUN=false
FORCE=false
while [ $# -gt 0 ]; do
    case "$1" in
        --from) BACKUP_REPO_OVERRIDE="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        --force) FORCE=true; shift ;;
        -h|--help)
            grep -E '^#( |$)' "$0" | sed 's/^# //; s/^#//'
            exit 0 ;;
        *)
            echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

# ── Status tracking ──────────────────────────────────────────────────
_STATUS_FILE="$HOME/.genesis/restore_status.json"
_STARTED_AT=$(date +%s)
_SQLITE_RESTORED=false
_QDRANT_RESTORED=0
_TRANSCRIPT_RESTORED=0
_MEMORY_RESTORED=0
_CCMEM_RESTORED=false
_OVERLAYS_RESTORED=0
_SECRETS_RESTORED=false
_SUCCESS=false
_FAILURES=()

_write_status() {
    local _ended_at
    _ended_at=$(date +%s)
    local _duration=$(( _ended_at - _STARTED_AT ))
    local _failures_json
    _failures_json=$(printf '%s\n' "${_FAILURES[@]:-}" | python3 -c "import json,sys; print(json.dumps([l for l in sys.stdin.read().splitlines() if l]))")
    mkdir -p "$(dirname "$_STATUS_FILE")"
    cat > "$_STATUS_FILE" <<STATUSEOF
{"timestamp":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","success":$_SUCCESS,"dry_run":$DRY_RUN,"sqlite_restored":$_SQLITE_RESTORED,"qdrant_restored":$_QDRANT_RESTORED,"transcripts_restored":$_TRANSCRIPT_RESTORED,"memory_restored":$_MEMORY_RESTORED,"cc_memory_restored":$_CCMEM_RESTORED,"overlays_restored":$_OVERLAYS_RESTORED,"secrets_restored":$_SECRETS_RESTORED,"duration_s":$_duration,"failures":$_failures_json}
STATUSEOF
}
trap '_write_status' EXIT

# ── Setup ────────────────────────────────────────────────────────────
GENESIS_DIR="${GENESIS_DIR:-$HOME/genesis}"
BACKUP_DIR="$HOME/backups/genesis-backups"
_CC_PROJECT_ID=$(echo "$GENESIS_DIR" | tr '/' '-')
MEMORY_DIR="$HOME/.claude/projects/${_CC_PROJECT_ID}/memory"
TRANSCRIPT_DIR="$HOME/.claude/projects/${_CC_PROJECT_ID}"
SECRETS_FILE="${SECRETS_PATH:-$GENESIS_DIR/secrets.env}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
LOG_PREFIX="[genesis-restore]"

log()  { echo "$LOG_PREFIX $(date -Iseconds) $*"; }
warn() { log "WARNING: $*"; _FAILURES+=("$*"); }
die()  { log "FATAL: $*"; _FAILURES+=("$*"); exit 1; }

_BACKUP_PASSPHRASE="${GENESIS_BACKUP_PASSPHRASE:-}"

# decrypt_file <src.gpg> <dst>
decrypt_file() {
    local src="$1" dst="$2"
    printf '%s' "$_BACKUP_PASSPHRASE" | gpg --batch --yes --passphrase-fd 0 \
        -d -o "$dst" "$src" 2>/dev/null
}

# read_payload <path-without-.gpg> → echo resolved path and whether decryption needed.
# Populates __PAYLOAD_SRC (to read) and __PAYLOAD_NEEDS_DECRYPT (true/false).
# Returns 1 if neither encrypted nor plaintext form exists.
resolve_payload() {
    local base="$1"
    if [ -f "${base}.gpg" ]; then
        __PAYLOAD_SRC="${base}.gpg"
        __PAYLOAD_NEEDS_DECRYPT=true
        return 0
    elif [ -f "$base" ]; then
        __PAYLOAD_SRC="$base"
        __PAYLOAD_NEEDS_DECRYPT=false
        return 0
    fi
    return 1
}

# Confirm prompt (skipped with --force or --dry-run).
confirm() {
    local prompt="$1"
    $FORCE && return 0
    $DRY_RUN && return 0
    read -r -p "$prompt [y/N] " reply
    [[ "$reply" =~ ^[yY]([eE][sS])?$ ]]
}

# ── Obtain backup repo ───────────────────────────────────────────────
if [ -n "$BACKUP_REPO_OVERRIDE" ]; then
    if [ -d "$BACKUP_REPO_OVERRIDE/.git" ] || [ -d "$BACKUP_REPO_OVERRIDE" ]; then
        # Treat as local path
        BACKUP_DIR="$BACKUP_REPO_OVERRIDE"
        log "Using backup source: $BACKUP_DIR"
    else
        log "Cloning backup repo from $BACKUP_REPO_OVERRIDE..."
        mkdir -p "$(dirname "$BACKUP_DIR")"
        $DRY_RUN || git clone "$BACKUP_REPO_OVERRIDE" "$BACKUP_DIR"
    fi
elif [ ! -d "$BACKUP_DIR/.git" ] && [ ! -d "$BACKUP_DIR" ]; then
    BACKUP_REPO="${GENESIS_BACKUP_REPO:-}"
    if [ -z "$BACKUP_REPO" ]; then
        die "Backup not found at $BACKUP_DIR and GENESIS_BACKUP_REPO unset. Pass --from <url-or-path>."
    fi
    log "Cloning backup repo..."
    mkdir -p "$(dirname "$BACKUP_DIR")"
    $DRY_RUN || git clone "$BACKUP_REPO" "$BACKUP_DIR"
fi

if [ -d "$BACKUP_DIR/.git" ]; then
    (cd "$BACKUP_DIR" && git pull --rebase --quiet 2>/dev/null) || log "git pull failed, continuing with local backup state"
fi

# Check encrypted payloads exist without passphrase → fail fast.
_has_encrypted=false
for candidate in "$BACKUP_DIR"/data/genesis.sql.gpg "$BACKUP_DIR"/secrets/secrets.env.gpg; do
    [ -f "$candidate" ] && _has_encrypted=true
done
if find "$BACKUP_DIR"/transcripts "$BACKUP_DIR"/memory "$BACKUP_DIR"/data/qdrant -name '*.gpg' -print -quit 2>/dev/null | grep -q .; then
    _has_encrypted=true
fi
if $_has_encrypted && [ -z "$_BACKUP_PASSPHRASE" ]; then
    die "Backup contains encrypted payloads but GENESIS_BACKUP_PASSPHRASE is unset"
fi

log "Mode: $( $DRY_RUN && echo dry-run || echo apply )  Force: $FORCE"

# ── 1. SQLite ────────────────────────────────────────────────────────
log "--- SQLite ---"
DB_FILE="$GENESIS_DIR/data/genesis.db"
if resolve_payload "$BACKUP_DIR/data/genesis.sql"; then
    src="$__PAYLOAD_SRC"
    if [ -f "$DB_FILE" ] && [ "$DB_FILE" -nt "$src" ] && ! $FORCE; then
        log "SQLite: destination is newer than backup — skipping (use --force to override)"
    else
        if $DRY_RUN; then
            log "SQLite: would restore from $src → $DB_FILE"
        elif confirm "Restore SQLite from $(basename "$src") into $DB_FILE?"; then
            mkdir -p "$(dirname "$DB_FILE")"
            _SQL_TMP=$(mktemp)
            if $__PAYLOAD_NEEDS_DECRYPT; then
                decrypt_file "$src" "$_SQL_TMP" || { warn "SQLite decrypt failed"; rm -f "$_SQL_TMP"; }
            else
                cp "$src" "$_SQL_TMP"
            fi
            if [ -s "$_SQL_TMP" ]; then
                # Back up the existing DB before we overwrite it.
                if [ -f "$DB_FILE" ]; then
                    cp "$DB_FILE" "${DB_FILE}.pre-restore.$(date +%s)"
                fi
                # Fresh DB from the SQL dump.
                rm -f "$DB_FILE"
                if command -v sqlite3 >/dev/null; then
                    if sqlite3 "$DB_FILE" ".read $_SQL_TMP"; then
                        _SQLITE_RESTORED=true
                        log "SQLite: restored → $DB_FILE"
                    else
                        warn "SQLite .read failed — inspect ${DB_FILE}.pre-restore.*"
                    fi
                else
                    warn "SQLite: sqlite3 binary not installed — cannot apply dump. Install sqlite3 and re-run."
                fi
            else
                warn "SQLite: dump payload is empty — backup may have been produced before sqlite3 was installed. Re-run backup.sh."
            fi
            rm -f "$_SQL_TMP"
        else
            log "SQLite: skipped (user declined)"
        fi
    fi
else
    log "SQLite: no backup payload found (neither genesis.sql.gpg nor genesis.sql)"
fi

# ── 2. Qdrant ────────────────────────────────────────────────────────
log "--- Qdrant ---"
if [ -d "$BACKUP_DIR/data/qdrant" ]; then
    # Verify Qdrant is reachable before we try anything.
    if ! curl -sf "$QDRANT_URL/" >/dev/null; then
        warn "Qdrant at $QDRANT_URL not reachable — skipping collection restore"
    else
        # Build a dedup'd collection → source map. When both .snapshot and
        # .snapshot.gpg exist for the same collection, prefer the encrypted
        # form (the plaintext is stale from a pre-encryption backup).
        declare -A _SNAPSHOTS
        while IFS= read -r -d '' snap; do
            name=$(basename "$snap")
            case "$name" in
                *.snapshot.gpg) coll="${name%.snapshot.gpg}" ;;
                *.snapshot)     coll="${name%.snapshot}" ;;
                *) continue ;;
            esac
            existing="${_SNAPSHOTS[$coll]:-}"
            if [ -z "$existing" ] || [[ "$snap" == *.gpg ]]; then
                _SNAPSHOTS[$coll]="$snap"
            fi
        done < <(find "$BACKUP_DIR/data/qdrant" -maxdepth 1 \
            \( -name '*.snapshot' -o -name '*.snapshot.gpg' \) -print0 2>/dev/null)

        for coll in "${!_SNAPSHOTS[@]}"; do
            snap="${_SNAPSHOTS[$coll]}"
            # If the collection already exists with points, don't clobber.
            existing_count=$(curl -sf "$QDRANT_URL/collections/$coll" 2>/dev/null \
                | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('result',{}).get('points_count',0))" 2>/dev/null \
                || echo "0")
            if [ "${existing_count:-0}" -gt 0 ] && ! $FORCE; then
                log "Qdrant: '$coll' has $existing_count points — skipping (use --force)"
                continue
            fi
            if $DRY_RUN; then
                log "Qdrant: would upload $(basename "$snap") → collection '$coll'"
                _QDRANT_RESTORED=$(( _QDRANT_RESTORED + 1 ))
                continue
            fi
            if ! confirm "Restore Qdrant collection '$coll' (this will recreate it)?"; then
                log "Qdrant: '$coll' skipped (user declined)"
                continue
            fi

            # Decrypt to tempfile if encrypted — curl -F needs a real fs path.
            upload_src="$snap"
            _QDRANT_TMP=""
            if [[ "$snap" == *.gpg ]]; then
                if [ -z "$_BACKUP_PASSPHRASE" ]; then
                    warn "Qdrant: '$coll' is encrypted but GENESIS_BACKUP_PASSPHRASE unset — skipping"
                    continue
                fi
                _QDRANT_TMP=$(mktemp --suffix=.snapshot)
                if ! decrypt_file "$snap" "$_QDRANT_TMP"; then
                    warn "Qdrant: decrypt failed for '$coll'"
                    rm -f "$_QDRANT_TMP"
                    continue
                fi
                upload_src="$_QDRANT_TMP"
            fi

            log "Qdrant: uploading '$coll' snapshot..."
            resp=$(curl -sf -X POST "$QDRANT_URL/collections/$coll/snapshots/upload?priority=snapshot" \
                -F "snapshot=@$upload_src" 2>/dev/null) || {
                warn "Qdrant: upload failed for '$coll'"
                [ -n "$_QDRANT_TMP" ] && rm -f "$_QDRANT_TMP"
                continue
            }
            [ -n "$_QDRANT_TMP" ] && rm -f "$_QDRANT_TMP"

            ok=$(echo "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin).get('result',False))" 2>/dev/null || echo false)
            if [ "$ok" = "True" ]; then
                _QDRANT_RESTORED=$(( _QDRANT_RESTORED + 1 ))
                post_count=$(curl -sf "$QDRANT_URL/collections/$coll" \
                    | python3 -c "import sys,json;print(json.load(sys.stdin)['result']['points_count'])" 2>/dev/null || echo "?")
                log "Qdrant: '$coll' restored ($post_count points)"
            else
                warn "Qdrant: upload returned non-ok for '$coll': $resp"
            fi
        done
    fi
else
    log "Qdrant: no snapshots in backup"
fi

# ── 3. CC transcripts ────────────────────────────────────────────────
log "--- Transcripts ---"
if [ -d "$BACKUP_DIR/transcripts" ]; then
    mkdir -p "$TRANSCRIPT_DIR"
    while IFS= read -r -d '' src; do
        name=$(basename "$src")
        # Strip .gpg if present to get dest name
        dst_name="${name%.gpg}"
        dst="$TRANSCRIPT_DIR/$dst_name"
        if [ -f "$dst" ] && [ "$dst" -nt "$src" ] && ! $FORCE; then
            continue
        fi
        if $DRY_RUN; then
            log "Transcripts: would restore $name → $dst"
            _TRANSCRIPT_RESTORED=$(( _TRANSCRIPT_RESTORED + 1 ))
            continue
        fi
        if [[ "$name" == *.gpg ]]; then
            decrypt_file "$src" "$dst" || { warn "transcript decrypt failed: $name"; continue; }
        else
            cp "$src" "$dst"
        fi
        _TRANSCRIPT_RESTORED=$(( _TRANSCRIPT_RESTORED + 1 ))
    done < <(find "$BACKUP_DIR/transcripts" -maxdepth 1 \( -name '*.jsonl' -o -name '*.jsonl.gpg' \) -print0 2>/dev/null)
    log "Transcripts: $_TRANSCRIPT_RESTORED restored"
else
    log "Transcripts: no backup directory"
fi

# ── 4. Auto-memory ───────────────────────────────────────────────────
log "--- Memory ---"
if [ -d "$BACKUP_DIR/memory" ]; then
    mkdir -p "$MEMORY_DIR"
    while IFS= read -r -d '' src; do
        rel="${src#$BACKUP_DIR/memory/}"
        dst_rel="${rel%.gpg}"
        dst="$MEMORY_DIR/$dst_rel"
        if [ -f "$dst" ] && [ "$dst" -nt "$src" ] && ! $FORCE; then
            continue
        fi
        if $DRY_RUN; then
            log "Memory: would restore $rel → $dst"
            _MEMORY_RESTORED=$(( _MEMORY_RESTORED + 1 ))
            continue
        fi
        mkdir -p "$(dirname "$dst")"
        if [[ "$src" == *.gpg ]]; then
            decrypt_file "$src" "$dst" || { warn "memory decrypt failed: $rel"; continue; }
        else
            cp "$src" "$dst"
        fi
        _MEMORY_RESTORED=$(( _MEMORY_RESTORED + 1 ))
    done < <(find "$BACKUP_DIR/memory" -type f -print0 2>/dev/null)
    log "Memory: $_MEMORY_RESTORED restored"
else
    log "Memory: no backup directory"
fi

# ── 5. In-repo CC memory backup ──────────────────────────────────────
log "--- CC memory (in-repo) ---"
# The backup.sh stores this under $GENESIS_DIR/data/cc-memory-backup (gitignored).
# There's a narrow restore_cc_memory.sh already — delegate to it if present.
CC_MEM_BACKUP="$GENESIS_DIR/data/cc-memory-backup"
if [ -x "$GENESIS_DIR/scripts/restore_cc_memory.sh" ] && [ -d "$CC_MEM_BACKUP" ]; then
    if $DRY_RUN; then
        log "CC memory: would run restore_cc_memory.sh"
        _CCMEM_RESTORED=true
    else
        bash "$GENESIS_DIR/scripts/restore_cc_memory.sh" "$GENESIS_DIR" \
            && _CCMEM_RESTORED=true \
            || warn "CC memory restore failed"
    fi
else
    log "CC memory: no cc-memory-backup dir or restore_cc_memory.sh — skipped"
fi

# ── 6. Local config overlays ─────────────────────────────────────────
log "--- Local config overlays ---"
if [ -d "$BACKUP_DIR/config_overrides" ]; then
    while IFS= read -r -d '' src; do
        name=$(basename "$src")
        dst="$GENESIS_DIR/config/$name"
        if [ -f "$dst" ] && [ "$dst" -nt "$src" ] && ! $FORCE; then
            continue
        fi
        if $DRY_RUN; then
            log "Overlay: would restore $name → $dst"
            _OVERLAYS_RESTORED=$(( _OVERLAYS_RESTORED + 1 ))
            continue
        fi
        mkdir -p "$(dirname "$dst")"
        cp "$src" "$dst"
        _OVERLAYS_RESTORED=$(( _OVERLAYS_RESTORED + 1 ))
    done < <(find "$BACKUP_DIR/config_overrides" -maxdepth 1 -name '*.local.yaml' -print0 2>/dev/null)
    log "Overlays: $_OVERLAYS_RESTORED restored"
else
    log "Overlays: no backup directory"
fi

# ── 7. Secrets ───────────────────────────────────────────────────────
log "--- Secrets ---"
SECRETS_SRC="$BACKUP_DIR/secrets/secrets.env.gpg"
if [ -f "$SECRETS_SRC" ]; then
    if [ -f "$SECRETS_FILE" ] && ! $FORCE; then
        log "Secrets: $SECRETS_FILE already exists — skipping (use --force to overwrite)"
    else
        if $DRY_RUN; then
            log "Secrets: would decrypt → $SECRETS_FILE"
        elif confirm "Decrypt secrets → $SECRETS_FILE?"; then
            mkdir -p "$(dirname "$SECRETS_FILE")"
            if decrypt_file "$SECRETS_SRC" "$SECRETS_FILE"; then
                chmod 0600 "$SECRETS_FILE"
                _SECRETS_RESTORED=true
                log "Secrets: decrypted → $SECRETS_FILE (chmod 0600)"
            else
                warn "Secrets: decrypt failed"
            fi
        else
            log "Secrets: skipped (user declined)"
        fi
    fi
else
    log "Secrets: no backup payload at $SECRETS_SRC"
fi

# ── Done ─────────────────────────────────────────────────────────────
if [ ${#_FAILURES[@]} -eq 0 ]; then
    _SUCCESS=true
    log "Restore complete"
else
    log "Restore complete with ${#_FAILURES[@]} warning(s):"
    for f in "${_FAILURES[@]}"; do log "  - $f"; done
    # Exit non-zero so CI / cron can flag partial restores.
    exit 1
fi
