#!/usr/bin/env bash
# Genesis automated backup — runs every 6h via the genesis-backup.timer
# systemd user unit. Unit files are installed by bootstrap.sh; enabling is a
# deliberate step once backup is configured (see SETUP.md "Backups"):
#   systemctl --user enable --now genesis-backup.timer
# Writes your Genesis state to <your-gh-user>/genesis-backups.
# All PII-bearing payloads are GPG-encrypted with GENESIS_BACKUP_PASSPHRASE.
#
# Backs up: SQLite DB, Qdrant snapshots, CC transcripts, auto-memory,
# local config overlays, secrets.
#
# Restore via scripts/restore.sh or `python -m genesis restore`.
#
# To scrub pre-encryption plaintext payloads from the backup repo's git
# history, run scripts/migrate-backup-history.sh (one-shot, user-invoked).
#
# Environment variables (all optional unless noted):
#   GENESIS_BACKUP_REPO        — Git URL for backup repo (auto-detected from existing clone)
#   GENESIS_BACKUP_PASSPHRASE  — GPG passphrase (REQUIRED for secrets + encrypted payloads)
#   GENESIS_DIR                — Genesis repo root (default: ~/genesis)
#   QDRANT_URL                 — Qdrant server URL (default: http://localhost:6333)
#   SECRETS_PATH               — Path to secrets.env (default: $GENESIS_DIR/secrets.env)
set -euo pipefail

# Pluggable Tier-2 (off-site) backend interface — selects none/local/smb at runtime
# (backward-compat: a configured GENESIS_BACKUP_NAS with no selector → smb).
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/backup_backends.sh
source "$_SCRIPT_DIR/lib/backup_backends.sh"

# ── Status tracking ──────────────────────────────────────────────────
_STATUS_FILE="$HOME/.genesis/backup_status.json"
_STARTED_AT=$(date +%s)
_SQLITE_LINES=0
_QDRANT_COUNT=0
_TRANSCRIPT_COUNT=0
_MEMORY_COUNT=0
_SECRETS_OK=false
_SUCCESS=false
_FAILURE_REASON=""

_write_status() {
    local _ended_at
    _ended_at=$(date +%s)
    local _duration=$(( _ended_at - _STARTED_AT ))
    # Escape failure reason for JSON safety (quotes, backslashes, newlines)
    local _safe_reason
    _safe_reason=$(printf '%s' "$_FAILURE_REASON" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\n/\\n/g')
    # offsite_confirmed: true only when the off-site copy fully succeeded.
    local _offsite_confirmed=false
    if [ "${_T2_STATUS:-}" = "ok" ]; then _offsite_confirmed=true; fi
    mkdir -p "$(dirname "$_STATUS_FILE")"
    cat > "$_STATUS_FILE" <<STATUSEOF
{"timestamp":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","success":$_SUCCESS,"sqlite_lines":$_SQLITE_LINES,"qdrant_collections":$_QDRANT_COUNT,"transcript_files":$_TRANSCRIPT_COUNT,"memory_files":$_MEMORY_COUNT,"secrets_encrypted":$_SECRETS_OK,"duration_s":$_duration,"failure_reason":"$_safe_reason","tier2_status":"${_T2_STATUS:-unknown}","offsite_confirmed":$_offsite_confirmed}
STATUSEOF
}
trap '_write_status; backend_cleanup' EXIT

GENESIS_DIR="${GENESIS_DIR:-$HOME/genesis}"
BACKUP_DIR="$HOME/backups/genesis-backups"
# Derive CC project dir from genesis dir path (CC convention: / → -)
_CC_PROJECT_ID=$(echo "$GENESIS_DIR" | tr '/' '-')
MEMORY_DIR="$HOME/.claude/projects/${_CC_PROJECT_ID}/memory"
TRANSCRIPT_DIR="$HOME/.claude/projects/${_CC_PROJECT_ID}"
SECRETS_FILE="${SECRETS_PATH:-$GENESIS_DIR/secrets.env}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
LOG_PREFIX="[genesis-backup]"

# Source secrets for backup passphrase (cron doesn't inherit shell env)
if [ -f "$SECRETS_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$SECRETS_FILE"
    set +a
fi

log() { echo "$LOG_PREFIX $(date -Iseconds) $*"; }

die() { _FAILURE_REASON="$*"; log "FATAL: $*"; exit 1; }

# Send a Telegram message (no-op unless bot token + chat id are configured).
# Shared by the backup-failed and off-site-replication-failed alerts.
_send_telegram() {
    [ -n "${TELEGRAM_BOT_TOKEN:-}" ] || return 0
    [ -n "${TELEGRAM_FORUM_CHAT_ID:-}" ] || return 0
    curl -sf -X POST \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "{\"chat_id\":\"${TELEGRAM_FORUM_CHAT_ID}\",\"text\":$(printf '%s' "$1" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))'),\"parse_mode\":\"Markdown\"}" \
        > /dev/null 2>&1 || log "WARNING: Telegram alert failed to send"
}

# Large intermediate files (the multi-hundred-MB SQLite .dump) must NOT land in the
# inherited TMPDIR: for a CC-launched run that is ~/.genesis/cc-tmp (the watchgod-policed
# "oxygen" folder — filling it kills CC sessions); for the 6h timer unit it is /tmp (tmpfs/RAM).
# Route them to a dedicated on-disk dir, per the tmp_filesystem_limit procedure ("use ~/tmp
# for large temporary files"). We do NOT export TMPDIR — only the big files move; everything
# else (and Claude Code) keeps its normal TMPDIR.
GENESIS_BIG_TMP="${GENESIS_BACKUP_TMPDIR:-$HOME/tmp}"
mkdir -p "$GENESIS_BIG_TMP"
log "big-temp dir: $GENESIS_BIG_TMP"

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
        _SQL_TMP=$(mktemp -p "$GENESIS_BIG_TMP")  # ~269MB dump — keep off cc-tmp/RAM
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

# --- 2. Qdrant snapshots (encrypted — snapshots contain embedding vectors
#       and payloads derived from memory/DB content) ---
log "Backing up Qdrant collections..."
mkdir -p data/qdrant
# Purge any pre-encryption plaintext snapshots so they don't persist
# alongside the new encrypted form.
find data/qdrant -maxdepth 1 -name '*.snapshot' -type f -delete 2>/dev/null || true
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

    # Encrypt. Refuse plaintext if no passphrase — Qdrant snapshots are
    # not opaque enough to ship plaintext to the backup repo.
    if ! $_ENCRYPT_READY; then
        log "WARNING: GENESIS_BACKUP_PASSPHRASE not set — skipping Qdrant snapshot for $collection (refusing plaintext)"
        rm -f "data/qdrant/${collection}.snapshot"
    elif encrypt_file "data/qdrant/${collection}.snapshot" "data/qdrant/${collection}.snapshot.gpg"; then
        rm -f "data/qdrant/${collection}.snapshot"
        _QDRANT_COUNT=$(( _QDRANT_COUNT + 1 ))
        log "Qdrant: $collection ($(du -sh "data/qdrant/${collection}.snapshot.gpg" | cut -f1), encrypted)"
    else
        log "WARNING: Qdrant encryption failed for $collection"
        rm -f "data/qdrant/${collection}.snapshot"
    fi

    # Clean up snapshot from Qdrant server
    curl -sf -X DELETE "$QDRANT_URL/collections/$collection/snapshots/$snapshot_name" >/dev/null 2>&1 || true
done

# --- 3. CC session transcripts (encrypted — contain conversation PII) ---
#
# RETENTION POLICY — KEEP FOREVER BY DEFAULT. Backed-up transcripts are Genesis's
# durable long-term conversational memory. The LOCAL store (~/.claude/projects)
# expires on Claude Code's cleanupPeriodDays, but the BACKUP is the permanent
# archive: transcripts/*.gpg accumulate here and are NEVER auto-pruned (the only
# delete below is the pre-encryption plaintext staging, not the .gpg archive).
# Any future retention work (GFS snapshot pruning, local-staging prune) MUST
# EXEMPT transcripts/ — a user may opt into expiry, but the system must never
# auto-expire transcripts the way the local store does.
log "Backing up CC transcripts..."
mkdir -p transcripts
# Purge any pre-encryption plaintext transcripts (staging only — NOT the .gpg).
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

# --- Tier 2: upload large files to the off-site backend (pluggable) ---
# Destination is selectable (none/local/smb) via GENESIS_BACKUP_TIER2_BACKEND —
# the public repo prescribes no provider. The dated snapshot tree
# (Genesis/<host>/<UTC-stamp>/{data,qdrant,transcripts}/ + COMPLETE marker) is
# written through the backend interface, not a backend binary directly. The
# _T2_STATUS value strings (ok/partial/not_configured/no_smbclient) are unchanged
# — they're read by the dashboard + health alerts.
_T2_STATUS="skipped"
backend_init
_T2_BACKEND="$(backend_name)"
if [ "$_T2_BACKEND" = "none" ]; then
    log "Tier 2 backup target not configured — large files are local-only"
    _T2_STATUS="not_configured"
elif ! backend_available; then
    if [ "$_T2_BACKEND" = "smb" ]; then
        log "WARNING: smbclient not installed — Tier 2 backup skipped"
        _T2_STATUS="no_smbclient"
    else
        log "WARNING: Tier 2 backend '$_T2_BACKEND' is not available — Tier 2 backup skipped"
        _T2_STATUS="not_configured"
    fi
else
    _T2_HOST_DIR="Genesis/$(hostname)"
    # Per-run DATED snapshot dir — a consistent point-in-time copy that restore.sh
    # selects the latest COMPLETE of (and GFS retention prunes).
    _T2_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
    _T2_DIR="${_T2_HOST_DIR}/${_T2_STAMP}"

    # Create the snapshot directory tree (backend_mkdir creates ancestors;
    # pre-existing levels are idempotent).
    backend_mkdir "${_T2_DIR}/data"
    backend_mkdir "${_T2_DIR}/qdrant"
    backend_mkdir "${_T2_DIR}/transcripts"

    _T2_OK=true

    # Upload Qdrant snapshots
    for f in data/qdrant/*.gpg; do
        [ -f "$f" ] || continue
        fname=$(basename "$f")
        if backend_put "$f" "${_T2_DIR}/qdrant/$fname"; then
            log "  off-site: uploaded $fname"
        else
            log "WARNING: off-site upload failed for $fname"
            _T2_OK=false
        fi
    done

    # Upload SQL dump
    if [ -f data/genesis.sql.gpg ]; then
        if backend_put "data/genesis.sql.gpg" "${_T2_DIR}/data/genesis.sql.gpg"; then
            log "  off-site: uploaded genesis.sql.gpg"
        else
            log "WARNING: off-site upload failed for genesis.sql.gpg"
            _T2_OK=false
        fi
    fi

    # Upload transcripts (part of the off-site snapshot)
    for f in transcripts/*.gpg; do
        [ -f "$f" ] || continue
        fname=$(basename "$f")
        if backend_put "$f" "${_T2_DIR}/transcripts/$fname"; then
            log "  off-site: uploaded transcripts/$fname"
        else
            log "WARNING: off-site upload failed for transcripts/$fname"
            _T2_OK=false
        fi
    done

    # Upload memory / config overlays / secrets — previously git-Tier-1 only. Including
    # them here makes the off-site snapshot a COMPLETE copy, so a no-git fresh-box DR can
    # rehydrate everything (restore.sh §4/§6/§7 read them from the pulled snapshot). Memory
    # is flat (MEMORY_DIR has no subdirs); config overlays ship as-is (plaintext, mirroring
    # the existing Tier-1 + private-repo posture); secrets is the encrypted blob. A failed
    # upload of a present file flips _T2_OK so COMPLETE is gated on these landing too.
    #
    # Enumerate with `find` (not a `*.gpg` shell glob): §4/§6 stage these via `find`, which
    # includes DOTFILES (e.g. a transient .consolidate-lock), so a glob would silently drop
    # leading-dot names and leave the off-site copy short of Tier-1. Process substitution
    # (not `find | while`) keeps the loop in THIS shell so the _T2_OK flip survives.
    backend_mkdir "${_T2_DIR}/memory"
    while IFS= read -r -d '' f; do
        fname=$(basename "$f")
        if backend_put "$f" "${_T2_DIR}/memory/$fname"; then
            log "  off-site: uploaded memory/$fname"
        else
            log "WARNING: off-site upload failed for memory/$fname"
            _T2_OK=false
        fi
    done < <(find memory -maxdepth 1 -type f -name '*.gpg' -print0 2>/dev/null)

    backend_mkdir "${_T2_DIR}/config_overrides"
    while IFS= read -r -d '' f; do
        fname=$(basename "$f")
        if backend_put "$f" "${_T2_DIR}/config_overrides/$fname"; then
            log "  off-site: uploaded config_overrides/$fname"
        else
            log "WARNING: off-site upload failed for config_overrides/$fname"
            _T2_OK=false
        fi
    done < <(find config_overrides -maxdepth 1 -type f -name '*.local.yaml' -print0 2>/dev/null)

    # Secrets is best-effort: a MISSING payload (no passphrase → no .gpg) is not an
    # off-site failure (that path already fails the backup via _SQLITE_LINES=0); only a
    # failed upload of a PRESENT payload flips _T2_OK.
    if [ -f secrets/secrets.env.gpg ]; then
        backend_mkdir "${_T2_DIR}/secrets"
        if backend_put "secrets/secrets.env.gpg" "${_T2_DIR}/secrets/secrets.env.gpg"; then
            log "  off-site: uploaded secrets/secrets.env.gpg"
        else
            log "WARNING: off-site upload failed for secrets/secrets.env.gpg"
            _T2_OK=false
        fi
    fi

    # Mark the snapshot COMPLETE only when every upload succeeded, so restore never
    # picks a half-uploaded snapshot (it selects the latest COMPLETE one). If the
    # marker itself fails to upload, the snapshot is unusable for restore — treat
    # that as an off-site failure (partial + alert), not ok.
    if [ "$_T2_OK" = true ]; then
        _T2_MARKER=$(mktemp)  # empty marker, uploaded only after a full snapshot
        if ! backend_put "$_T2_MARKER" "${_T2_DIR}/COMPLETE"; then
            log "WARNING: off-site upload failed for COMPLETE marker — snapshot unusable for restore"
            _T2_OK=false
        fi
        rm -f "$_T2_MARKER"
    fi

    if [ "$_T2_OK" = true ]; then
        _T2_STATUS="ok"
        log "Tier 2 backup copied to off-site snapshot ${_T2_STAMP} (backend: ${_T2_BACKEND})"
    else
        _T2_STATUS="partial"
        log "WARNING: Tier 2 backup partially failed"
    fi

    # --- Tier 2 GFS retention prune (off-site dated snapshots only) --------------
    # Keep daily 7 / weekly 4 / monthly 6 of the COMPLETE off-site snapshots. gfs_select
    # ALWAYS keeps the newest (restore.sh selects the latest COMPLETE); we also skip the
    # current run's stamp explicitly. Best-effort — a prune failure never fails the backup.
    # Transcripts are preserved elsewhere (local git keep-forever + the latest snapshot
    # re-uploads the full set every run), so deleting an aged snapshot's transcripts/ copy
    # loses nothing. Only the off-site dated tree is touched; the local ~/backups git repo
    # is never pruned here. Runs only after a fully-uploaded (ok) snapshot this run.
    if [ "${_T2_STATUS:-}" = "ok" ] && backend_available; then
        # DR-safety: the host segment flows into a DESTRUCTIVE backend_delete (smb deltree /
        # local rm -rf), so refuse to prune unless it is the plain filename charset — then a
        # pathological hostname can never break out of the snapshot path. (The stamps below
        # are already grep-restricted to the timestamp charset.)
        if ! printf '%s' "$_T2_HOST_DIR" | grep -qE '^Genesis/[A-Za-z0-9._-]+$'; then
            log "WARNING: GFS prune skipped — unsafe off-site host path '$_T2_HOST_DIR'"
        else
            _gfs_complete="$(
                for _st in $(backend_list_dirs "$_T2_HOST_DIR" 2>/dev/null \
                                | grep -oE '[0-9]{8}T[0-9]{6}Z' | sort -u || true); do
                    backend_exists "$_T2_HOST_DIR/$_st/COMPLETE" && printf '%s\n' "$_st"
                done
                true
            )"
            _gfs_delete="$(printf '%s\n' "$_gfs_complete" \
                | python3 "$_SCRIPT_DIR/gfs_select.py" --daily 7 --weekly 4 --monthly 6)" || _gfs_delete=""
            for _st in $_gfs_delete; do
                if [ "$_st" = "$_T2_STAMP" ]; then
                    continue   # never the current run's snapshot
                fi
                if backend_delete "$_T2_HOST_DIR/$_st"; then
                    log "GFS prune: removed off-site snapshot $_st"
                else
                    log "WARNING: GFS prune failed for off-site snapshot $_st"
                fi
            done
        fi
    fi
fi
backend_cleanup

# --- Ensure .gitignore excludes Tier 2 files ---
# Tier 1 (git): memory/, config_overrides/, secrets/
# Tier 2 (off-site): data/, transcripts/
if ! grep -q '^data/$' .gitignore 2>/dev/null; then
    cat >> .gitignore << 'GITIGNORE'
# Tier 2 files — backed up off-site, not GitHub
data/
transcripts/
GITIGNORE
    log "Added Tier 2 exclusions to .gitignore"
fi

# --- Commit and push (Tier 1 only) ---
log "Committing backup..."
git add -A
if git diff --cached --quiet; then
    log "No changes since last backup"
else
    # Explicit error handling — set -e is suppressed by ||.
    # Without this, a corrupt git repo silently kills the script
    # (as happened 2026-05-08 through 2026-05-25: 17 days unnoticed).
    if git commit -m "backup: $(date -Iseconds)" --quiet 2>&1; then
        if ! git push --quiet 2>&1; then
            _FAILURE_REASON="git push failed — backup exists locally only (not replicated to remote)"
            log "ERROR: $_FAILURE_REASON"
        else
            log "Backup committed and pushed"
        fi
    else
        _FAILURE_REASON="git commit failed (corrupt repo or index error)"
        log "ERROR: git commit failed — repository may need re-clone from remote"
    fi
fi

if [ "$_SQLITE_LINES" -gt 0 ] && [ -z "$_FAILURE_REASON" ]; then
    _SUCCESS=true
else
    if [ -z "$_FAILURE_REASON" ]; then
        _FAILURE_REASON="No SQLite data backed up"
    fi
    log "WARNING: Backup incomplete — marking as failure (reason: $_FAILURE_REASON)"
fi

# --- Alert on failure via Telegram ---
if [ "$_SUCCESS" != "true" ]; then
    _send_telegram "🚨 *Backup failed*

Reason: ${_FAILURE_REASON:-unknown}
Time: $(date -Is)
SQLite lines: $_SQLITE_LINES
Duration: $(( $(date +%s) - _STARTED_AT ))s"
fi

# --- Alert on off-site replication failure (local backup still OK) ---
# Distinct from a backup failure: the local + Tier-1 backup succeeded, but the
# off-site copy did not fully land. Local-only installs (no off-site backend) are
# a valid choice and do NOT alert.
if [ "${_T2_BACKEND:-none}" != "none" ] && [ "$_T2_STATUS" != "ok" ]; then
    # Dedup: alert only on the transition INTO off-site failure. The status file
    # still holds the PREVIOUS run's state at this point (the EXIT trap rewrites
    # it after). This avoids a 6-hourly alert while the off-site target stays down; it re-alerts
    # once off-site recovers (offsite_confirmed flips true) and then fails again.
    _prev_offsite=$(python3 -c "import json; print(json.load(open('$_STATUS_FILE')).get('offsite_confirmed', True))" 2>/dev/null || echo "True")
    if [ "$_prev_offsite" != "False" ]; then
        _send_telegram "⚠️ *Off-site replication failed*

The local backup is OK, but it was NOT replicated off-site.
Tier-2 status: ${_T2_STATUS}
Time: $(date -Is)
Off-site copies are missing — check the off-site target."
    fi
fi
log "Backup complete (success=$_SUCCESS)"
