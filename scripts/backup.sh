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
#   GENESIS_BACKUP_NAS_HOST    — Tier-2 off-site host dir label under Genesis/
#                                (default: $(hostname)). SET THIS to a distinct
#                                value when two machines share a hostname and back
#                                up to the same NAS, or their GFS prunes delete
#                                each other's snapshots. Read symmetrically by
#                                restore.sh to locate the source snapshot dir.
set -euo pipefail

# Pluggable Tier-2 (off-site) backend interface — selects none/local/smb at runtime
# (backward-compat: a configured GENESIS_BACKUP_NAS with no selector → smb).
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/backup_backends.sh
source "$_SCRIPT_DIR/lib/backup_backends.sh"
# Durable alert queue (F.3): if a Telegram alert can't send, persist it so the
# container drainer delivers it on recovery instead of losing it to a log line.
# Guarded no-op fallback if the lib is ever not co-located.
if [ -f "$_SCRIPT_DIR/lib/alert_queue.sh" ]; then
    # shellcheck source=scripts/lib/alert_queue.sh
    source "$_SCRIPT_DIR/lib/alert_queue.sh"
else
    queue_alert() { :; }
fi

# ── Mutual exclusion (SF5): backup↔restore share one whole-run lock ──
# Non-blocking: a 6h timer run SKIPS (exit 0) when a restore — or another
# backup — holds the lock; it must never queue behind a multi-minute DR
# restore. The skip deliberately does NOT touch backup_status.json: writing
# success:false would page a false CRITICAL (health `backup:last_failed`)
# during a legitimate restore, so the prior run's status stays put and the
# existing `backup:overdue` staleness alert (>8h) detects a wedged holder
# honestly. That is why this block sits BEFORE the EXIT trap below — a skip
# must write nothing (log() isn't defined yet either; the echo is inline).
# The lock fd is held for the script's lifetime and released at exit; the
# open is APPEND mode so a losing contender never truncates the holder line
# (only the winner rewrites it, by path, after acquiring).
# NOTE deliberately NOT checked here: ~/.genesis/update_in_progress.pid —
# update.sh invokes this script AS its pre-update backup while the dashboard
# orchestrator holds that marker; honoring it would silently skip every
# pre-deploy backup while update.sh prints "Backup complete".
# shellcheck source=scripts/lib/dr_lock.sh
source "$_SCRIPT_DIR/lib/dr_lock.sh"
dr_lock_open
if ! flock -n "$DR_LOCK_FD"; then
    _holder="$(cat "$DR_LOCK_FILE" 2>/dev/null || true)"
    echo "[genesis-backup] $(date -Iseconds) SKIPPED: backup-restore lock held by ${_holder:-unknown} — backup not run"
    exit 0
fi
dr_lock_stamp backup

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
# SF3 freshness tracking: only payloads regenerated THIS run may enter the
# off-site dated snapshot — a leftover .gpg from a prior run must never be
# re-badged under a fresh COMPLETE stamp (it silently misrepresents recency,
# and GFS retention then ages out the snapshots holding genuinely-fresh data).
_SQL_FRESH=false        # SQL dump regenerated (encrypted) this run
_SQL_RESTORABLE=false   # AND round-trip-verified → eligible for off-site COMPLETE
_SQL_ESCROW_DRIFT=false # env-decryptable but escrow stale → off-site DR degraded
_QDRANT_FRESH=""    # space-separated collections snapshotted+encrypted this run
_QDRANT_FAILED=""   # collections that EXIST (HTTP 200) but failed to snapshot this run
# Tier-1 replication: true once the local repo is in sync with the GitHub remote.
_TIER1_PUSHED=false
# Off-site snapshot bookkeeping. _T2_SNAPSHOT_COUNT / _T2_PRUNED stay UNSET until
# the GFS prune runs (only on a fully-uploaded off-site snapshot), so the status
# line emits a JSON `null` (not 0) when off-site was skipped/partial — an honest
# "unknown", and never an empty expansion (which would be invalid JSON).

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
{"timestamp":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","success":$_SUCCESS,"sqlite_lines":$_SQLITE_LINES,"qdrant_collections":$_QDRANT_COUNT,"transcript_files":$_TRANSCRIPT_COUNT,"memory_files":$_MEMORY_COUNT,"eval_files":${_EVAL_COUNT:-0},"secrets_encrypted":$_SECRETS_OK,"duration_s":$_duration,"failure_reason":"$_safe_reason","tier2_status":"${_T2_STATUS:-unknown}","offsite_confirmed":$_offsite_confirmed,"tier2_backend":"${_T2_BACKEND:-none}","snapshot_id":"${_T2_STAMP:-}","snapshot_count":${_T2_SNAPSHOT_COUNT:-null},"pruned_count":${_T2_PRUNED:-null},"tier1_pushed":$_TIER1_PUSHED}
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

# Load secrets for the backup passphrase (cron doesn't inherit shell
# env) WITHOUT shell-evaluating the file — `source` would execute any
# command substitution embedded in a value.
# shellcheck source=scripts/lib/load_secrets.sh
source "$_SCRIPT_DIR/lib/load_secrets.sh"
load_secrets_file "$SECRETS_FILE"

# Escrow lookup for the SF4 round-trip (shared with restore.sh).
# shellcheck source=scripts/lib/passphrase_escrow.sh
source "$_SCRIPT_DIR/lib/passphrase_escrow.sh"

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
        > /dev/null 2>&1 || {
        # Send failed (network/token) — don't lose the alert. Content-derived
        # dedupe key so distinct backup alerts stay separate but identical
        # repeats collapse to one queued entry.
        local _dkey
        _dkey="backup:$(printf '%s' "$1" | md5sum 2>/dev/null | cut -c1-12)"
        log "WARNING: Telegram alert failed to send — queued for retry"
        queue_alert emergency "backup" "Backup alert (Telegram send failed)" "$1" "$_dkey"
    }
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

# Git network ops run while the backup↔restore lock is held for the whole run,
# so an unbounded stall (half-open TCP to the remote — kernel retransmit can
# hang 10-15min) would block a concurrent DR restore past its wait. Bound them
# (named failure: stalled push/pull/clone holding the DR lock). GENESIS-
# overridable for slow links; -k SIGKILLs a SIGTERM-ignoring git.
_GIT_NET_TIMEOUT="${GENESIS_BACKUP_GIT_TIMEOUT:-300}"
_git_net() { timeout -k 10 "$_GIT_NET_TIMEOUT" git "$@"; }

# _roundtrip_ok <passphrase> <artifact.gpg> [stderr_file] — decrypt-verify that
# <artifact.gpg> decrypts with <passphrase> AND ends with the sqlite `.dump`
# success marker `COMMIT;` (a mid-dump failure ends `ROLLBACK; -- due to
# errors`). Returns 0 on verified, 1 otherwise. Streams (no plaintext temp).
# Captures the decrypt tail into a var FIRST so `grep -q` matching early can't
# SIGPIPE `tail` and flip the pipeline non-zero under pipefail on a good dump.
_roundtrip_ok() {
    local _pass="$1" _art="$2" _errf="${3:-/dev/null}" _tailf _rc _grc
    _tailf=$(mktemp -p "$GENESIS_BIG_TMP")
    # Real pipeline (NOT inside $()) so PIPESTATUS reflects gpg's own exit; tail
    # buffers to a file (not piped into grep) so nothing can SIGPIPE gpg/tail and
    # flip the result under pipefail. tail -5 reads to EOF, so gpg always runs
    # fully and its exit at PIPESTATUS[1] (printf=0, gpg=1, tail=2) is authoritative.
    printf '%s' "$_pass" | gpg --batch --passphrase-fd 0 -d "$_art" 2>"$_errf" | tail -5 > "$_tailf"
    _rc=${PIPESTATUS[1]}
    if [ "$_rc" -ne 0 ]; then rm -f "$_tailf"; return 1; fi
    grep -q '^COMMIT;' "$_tailf"; _grc=$?
    rm -f "$_tailf"
    return "$_grc"
}

# _verify_sql_roundtrip <artifact.gpg> — classify the freshly-encrypted SQL
# archive's restorability and echo one verdict word:
#   RESTORABLE — decrypts with the passphrase a DR box would use (escrow if
#                present, else env). Ships off-site, normal success.
#   DRIFT      — decrypts with the ENV passphrase but NOT the escrowed one
#                (secrets.env rotated, escrow stale). The LOCAL backup is fine
#                (env-decryptable, and secrets.env is itself backed up), so this
#                is NOT a backup failure — but a real DR box (env gone) decrypts
#                with escrow and would fail, so the artifact is withheld from the
#                off-site COMPLETE snapshot and the caller surfaces "re-escrow".
#   CORRUPT    — does not decrypt with the ENV passphrase it was encrypted with
#                → the archive is damaged (bad GPG MDC / truncation). Hard fail.
# Happy path is ONE decrypt (escrow present & matches, or env-only & good); the
# second decrypt runs only to classify a first-decrypt failure.
_verify_sql_roundtrip() {
    local _art="$1" _errf
    _errf=$(mktemp -p "$GENESIS_BIG_TMP")
    passphrase_escrow_lookup
    local _primary="$_BACKUP_PASSPHRASE" _have_escrow=false
    if [ -n "$ESCROW_PASSPHRASE" ]; then _primary="$ESCROW_PASSPHRASE"; _have_escrow=true; fi
    if _roundtrip_ok "$_primary" "$_art" "$_errf"; then
        rm -f "$_errf"
        echo RESTORABLE
        return 0
    fi
    # Primary failed. If the primary WAS escrow, an env-passphrase success means
    # drift (artifact good, escrow stale); env failure means genuine corruption.
    if $_have_escrow && _roundtrip_ok "$_BACKUP_PASSPHRASE" "$_art" /dev/null; then
        rm -f "$_errf"
        echo DRIFT
        return 0
    fi
    # Surface the gpg error, sanitized to printable ASCII: _write_status only
    # escapes quotes/backslashes, so a raw tab/CR/UTF-8 gpg message would make
    # backup_status.json invalid — and the health consumer's read_text()+
    # json.loads would then throw UnicodeDecodeError (uncaught → crashes health)
    # or JSONDecodeError (swallowed → suppresses the very CRITICAL this raises).
    ROUNDTRIP_DETAIL=$(LC_ALL=C tr -cd '[:print:]' < "$_errf" | cut -c1-200)
    rm -f "$_errf"
    echo CORRUPT
    return 0
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
    _git_net clone "$BACKUP_REPO" "$BACKUP_DIR"
fi

cd "$BACKUP_DIR"

# Ensure git identity is configured (per-repo, not global)
git config user.name "Genesis Backup" 2>/dev/null || true
git config user.email "backup@genesis.local" 2>/dev/null || true
# Keep gc in-process: a detached auto-gc (gc.autoDetach default) inherits the
# held backup-restore lock fd and would hold the DR lock past script exit.
git config gc.autoDetach false 2>/dev/null || true

_git_net pull --rebase --quiet 2>/dev/null || log "WARNING: git pull failed, continuing with local state"

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
                _SQL_FRESH=true
                log "SQLite: $_SQLITE_LINES lines (encrypted)"
                # SF4 round-trip: classify the fresh artifact's restorability
                # (see _verify_sql_roundtrip). ROUNDTRIP_DETAIL is set (sanitized)
                # only on CORRUPT. _SQL_RESTORABLE gates the OFF-SITE upload so a
                # DR box never auto-selects a COMPLETE snapshot it can't decrypt.
                ROUNDTRIP_DETAIL=""
                case "$(_verify_sql_roundtrip data/genesis.sql.gpg)" in
                    RESTORABLE)
                        _SQL_RESTORABLE=true
                        log "SQLite: round-trip decrypt verified"
                        ;;
                    DRIFT)
                        # Local backup is fine (env-decryptable + secrets.env is
                        # itself backed up), so NOT a backup failure — but a real
                        # DR box decrypts with the stale escrow and would fail, so
                        # withhold this dump from the off-site COMPLETE snapshot
                        # (the last-good off-site copy, encrypted under the
                        # pre-rotation passphrase == the escrow, stays restorable)
                        # and surface a distinct re-escrow alert via the off-site
                        # path (never CRITICAL "backup failed").
                        _SQL_RESTORABLE=false
                        _SQL_ESCROW_DRIFT=true
                        log "WARNING: SQL round-trip failed with the ESCROWED passphrase but SUCCEEDED with the env one — escrow is stale (secrets.env rotated?). Local backup OK; off-site DR degraded until re-escrow."
                        ;;
                    *)  # CORRUPT
                        _SQL_RESTORABLE=false
                        _FAILURE_REASON="${_FAILURE_REASON:+$_FAILURE_REASON; }SQL archive failed round-trip decrypt with its own env passphrase (${ROUNDTRIP_DETAIL:-no gpg output}) — corrupt/unrestorable, withheld from off-site"
                        log "WARNING: $_FAILURE_REASON"
                        _SQLITE_LINES=0
                        ;;
                esac
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
    # SF3 existence probe, branched on the HTTP code: only a real 404 is the
    # benign "collection absent" skip. Connection-refused/timeout/5xx mean the
    # SERVER is unreachable — with the old `curl -sf || continue` those read
    # as "may not exist" and every backup reported success with zero fresh
    # Qdrant payloads, forever (the audit's exact hole). --max-time 10: a
    # localhost liveness GET, not a transfer.
    _probe_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
        "$QDRANT_URL/collections/$collection" 2>/dev/null || echo 000)
    if [ "$_probe_code" = "404" ]; then
        log "Qdrant: collection $collection does not exist — skipping"
        continue
    elif [ "$_probe_code" != "200" ]; then
        # Server unreachable/erroring (000/timeout/5xx) — we CANNOT confirm the
        # collection exists, so this is NOT the audit's "collections exist but
        # weren't captured" failure. Qdrant is rebuildable from the (round-trip-
        # verified) SQLite dump, and restore.sh likewise treats an unreachable
        # Qdrant as a skip — so degrade gracefully (WARNING, backup still
        # succeeds) instead of paging CRITICAL every 6h forever on a host where
        # Qdrant is optional, still booting, or briefly down. Only a REACHABLE
        # collection (HTTP 200) that then fails to snapshot is a hard failure.
        log "WARNING: Qdrant unreachable probing $collection (HTTP $_probe_code) — skipping (rebuildable from SQL)"
        continue
    fi
    # Create snapshot via Qdrant API. --max-time 600: snapshot creation is a
    # server-side write of the full collection (~282MB and growing); bounded
    # so a wedged Qdrant can't hold the DR lock forever, sized ~10x a healthy
    # local snapshot write.
    snapshot_resp=$(curl -sf --max-time 600 -X POST "$QDRANT_URL/collections/$collection/snapshots" 2>/dev/null) || {
        log "WARNING: Qdrant snapshot creation failed for $collection"
        _QDRANT_FAILED="$_QDRANT_FAILED $collection(create)"
        continue
    }
    snapshot_name=$(echo "$snapshot_resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['name'])" 2>/dev/null) || {
        log "WARNING: Could not parse snapshot response for $collection"
        _QDRANT_FAILED="$_QDRANT_FAILED $collection(parse)"
        continue
    }
    # Download snapshot. --max-time 900: a localhost transfer of the full
    # collection; bounded against a hung server, generous for a healthy one.
    curl -sf --max-time 900 "$QDRANT_URL/collections/$collection/snapshots/$snapshot_name" \
        -o "data/qdrant/${collection}.snapshot" 2>/dev/null || {
        log "WARNING: Could not download snapshot for $collection"
        _QDRANT_FAILED="$_QDRANT_FAILED $collection(download)"
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
        _QDRANT_FRESH="$_QDRANT_FRESH $collection"
        log "Qdrant: $collection ($(du -sh "data/qdrant/${collection}.snapshot.gpg" | cut -f1), encrypted)"
    else
        log "WARNING: Qdrant encryption failed for $collection"
        _QDRANT_FAILED="$_QDRANT_FAILED $collection(encrypt)"
        rm -f "data/qdrant/${collection}.snapshot"
    fi

    # Clean up snapshot from Qdrant server. --max-time 60: a local best-effort
    # DELETE (failure mode: a wedged Qdrant leaves the server-side snapshot to
    # its own retention — non-fatal, `|| true`); bounded only so a hung server
    # can't stall the backup here.
    curl -sf --max-time 60 -X DELETE "$QDRANT_URL/collections/$collection/snapshots/$snapshot_name" >/dev/null 2>&1 || true
done
# A collection that EXISTS but produced no fresh payload is a backup failure
# (success:false → CRITICAL + Telegram), not a quiet gap: restore auto-selects
# the newest COMPLETE snapshot, so silence here would let stale-or-missing
# vectors masquerade as current until a disaster surfaces it.
if [ -n "$_QDRANT_FAILED" ]; then
    _FAILURE_REASON="${_FAILURE_REASON:+$_FAILURE_REASON; }Qdrant backup failed for:${_QDRANT_FAILED}"
fi

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

# --- 6b. Infrastructure body schema (Tier 1) ---
# profile.json is regenerable, but annotations.json is LLM-spent judgment and
# the rendered doc keeps restores self-describing. Small (<100KB), plain copy —
# the backups repo is private.
if [ -d "$HOME/.genesis/infrastructure" ]; then
    log "Backing up infrastructure profile..."
    mkdir -p infrastructure
    for _f in profile.json annotations.json INFRASTRUCTURE.md; do
        [ -f "$HOME/.genesis/infrastructure/$_f" ] && cp "$HOME/.genesis/infrastructure/$_f" infrastructure/
    done
fi

# --- 6c. Eval golden sets (encrypted — hand-graded rubric calibration data
# holding recalled memory content: PII-bearing like section 4, and expensive
# to recreate. Install-local (~/.genesis/eval), absent on a fresh install. ---
_EVAL_DIR="$HOME/.genesis/eval"
if [ -d "$_EVAL_DIR" ]; then
    log "Backing up eval golden sets..."
    mkdir -p eval
    # Purge any pre-encryption plaintext.
    find eval -type f ! -name '*.gpg' -delete 2>/dev/null || true
    if ! $_ENCRYPT_READY; then
        log "WARNING: GENESIS_BACKUP_PASSPHRASE not set — skipping eval (refusing plaintext)"
    else
        while IFS= read -r -d '' src; do
            rel="${src#"$_EVAL_DIR"/}"
            dst="eval/${rel}.gpg"
            mkdir -p "$(dirname "$dst")"
            if [ -f "$dst" ] && [ "$dst" -nt "$src" ]; then
                continue
            fi
            encrypt_file "$src" "$dst" || log "WARNING: failed to encrypt eval/$rel"
        done < <(find "$_EVAL_DIR" -type f -print0)
        _EVAL_COUNT=$(find eval -type f -name '*.gpg' 2>/dev/null | wc -l)
        log "Eval golden sets: $_EVAL_COUNT files (encrypted)"
    fi
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

# --- 8. Critical credential & wiring files (encrypted, Tier 1) ---
# Small, high-value files whose loss means "reprovision from scratch" instead of
# "restore": SSH keys (incl. the guardian control-plane key), the gh + Claude
# Code credentials, and the host/network wiring config. Each is GPG-encrypted;
# missing files are skipped (non-fatal). Refuses plaintext without a passphrase.
log "Backing up critical credential & wiring files (encrypted)..."
mkdir -p creds
_CREDS_COUNT=0
if $_ENCRYPT_READY; then
    # Whole ~/.ssh (keys, config, known_hosts) — regular files only.
    if [ -d "$HOME/.ssh" ]; then
        mkdir -p creds/ssh
        while IFS= read -r -d '' _f; do
            _base="$(basename "$_f")"
            if encrypt_file "$_f" "creds/ssh/${_base}.gpg"; then
                _CREDS_COUNT=$(( _CREDS_COUNT + 1 ))
            else
                log "WARNING: failed to encrypt ssh/${_base}"
            fi
        done < <(find "$HOME/.ssh" -maxdepth 1 -type f -print0)
    fi
    # Named single files → creds/<flatname>.gpg
    for _spec in \
        "$HOME/.config/gh/hosts.yml:gh_hosts.yml" \
        "$HOME/.claude/.credentials.json:claude_credentials.json" \
        "$HOME/.claude.json:claude.json" \
        "$HOME/.genesis/guardian_remote.yaml:guardian_remote.yaml" \
        "$HOME/.genesis/config/genesis.yaml:genesis.yaml"; do
        _srcf="${_spec%%:*}"; _dstn="${_spec##*:}"
        [ -f "$_srcf" ] || continue
        if encrypt_file "$_srcf" "creds/${_dstn}.gpg"; then
            _CREDS_COUNT=$(( _CREDS_COUNT + 1 ))
        else
            log "WARNING: failed to encrypt ${_dstn}"
        fi
    done
    log "Creds: $_CREDS_COUNT files (encrypted)"
else
    log "WARNING: GENESIS_BACKUP_PASSPHRASE not set — skipping creds backup (refusing plaintext)"
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
    # Off-site host dir. Defaults to $(hostname), but an explicit
    # GENESIS_BACKUP_NAS_HOST override is REQUIRED when two machines share a
    # hostname AND a Tier-2 target: the GFS prune below deletes stale COMPLETE
    # snapshots under _T2_HOST_DIR, so two same-hostname machines writing to one
    # NAS would prune each other's history. Symmetric with restore.sh, which
    # already reads GENESIS_BACKUP_NAS_HOST to locate the source snapshot dir.
    _T2_HOST_DIR="Genesis/${GENESIS_BACKUP_NAS_HOST:-$(hostname)}"
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

    # Upload Qdrant snapshots — FRESH ones only (SF3). A .gpg left on disk by
    # a prior run (this run's snapshot failed) must not be stamped into a new
    # dated snapshot: it would misrepresent recency, and GFS retention would
    # age out the snapshots holding the genuinely-fresh copy. The stale local
    # file is kept (last-good copy); the failure already pages via
    # _QDRANT_FAILED above.
    for f in data/qdrant/*.gpg; do
        [ -f "$f" ] || continue
        fname=$(basename "$f")
        _coll="${fname%.snapshot.gpg}"
        case " $_QDRANT_FRESH " in
            *" $_coll "*) ;;
            *)
                log "WARNING: $fname is stale (not regenerated this run) — excluded from off-site snapshot"
                continue
                ;;
        esac
        if backend_put "$f" "${_T2_DIR}/qdrant/$fname"; then
            log "  off-site: uploaded $fname"
        else
            log "WARNING: off-site upload failed for $fname"
            _T2_OK=false
        fi
    done

    # Upload SQL dump — freshness AND restorability gated (SF3 + SF4). The
    # off-site snapshot is what a fresh DR box auto-selects, so it must only
    # ever carry a dump that box can actually decrypt: _SQL_RESTORABLE is true
    # only when the round-trip verified with the DR passphrase (escrow if
    # present). A stale (not-regenerated) OR round-trip-failed (corrupt, or
    # escrow-drift → DR box can't decrypt) dump is withheld, forcing NO COMPLETE
    # this run so restore keeps falling back to the last verified-good snapshot.
    if [ -f data/genesis.sql.gpg ] && $_SQL_FRESH && $_SQL_RESTORABLE; then
        if backend_put "data/genesis.sql.gpg" "${_T2_DIR}/data/genesis.sql.gpg"; then
            log "  off-site: uploaded genesis.sql.gpg"
        else
            log "WARNING: off-site upload failed for genesis.sql.gpg"
            _T2_OK=false
        fi
    elif [ -f data/genesis.sql.gpg ] && $_SQL_FRESH; then
        # Regenerated but not restorable with the DR passphrase — do NOT stamp a
        # COMPLETE snapshot around an undecryptable/corrupt dump.
        log "WARNING: genesis.sql.gpg failed round-trip verify — withheld from off-site (no COMPLETE this run; last-good retained)"
        _T2_OK=false
    elif [ -f data/genesis.sql.gpg ]; then
        log "WARNING: genesis.sql.gpg is stale (not regenerated this run) — excluded from off-site snapshot"
        _T2_OK=false
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

    # Upload eval golden sets (§6c) into the COMPLETE off-site snapshot so a
    # no-git fresh-box DR rehydrates them (restore §4b) — without this they were
    # Tier-1-git-only. Encrypted; nests under eval/golden/, so mirror the
    # relative path (backend_mkdir is idempotent). A failed upload of a present
    # file flips _T2_OK, same contract as the payloads above.
    if [ -d eval ]; then
        backend_mkdir "${_T2_DIR}/eval"
        while IFS= read -r -d '' f; do
            rel="${f#eval/}"
            _sub="$(dirname "$rel")"
            [ "$_sub" != "." ] && backend_mkdir "${_T2_DIR}/eval/${_sub}"
            if backend_put "$f" "${_T2_DIR}/eval/${rel}"; then
                log "  off-site: uploaded eval/${rel}"
            else
                log "WARNING: off-site upload failed for eval/${rel}"
                _T2_OK=false
            fi
        done < <(find eval -type f -name '*.gpg' -print0 2>/dev/null)
    fi

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

    # Upload creds (encrypted credential + wiring files) so the COMPLETE snapshot
    # genuinely includes them — a no-git fresh box pulls them from here (restore
    # §8). One level of nesting: creds/*.gpg + creds/ssh/*.gpg; a failed upload of
    # a present file flips _T2_OK, same contract as the payloads above.
    if [ -d creds ]; then
        backend_mkdir "${_T2_DIR}/creds"
        [ -d creds/ssh ] && backend_mkdir "${_T2_DIR}/creds/ssh"
        while IFS= read -r -d '' f; do
            rel="${f#creds/}"
            if backend_put "$f" "${_T2_DIR}/creds/${rel}"; then
                log "  off-site: uploaded creds/${rel}"
            else
                log "WARNING: off-site upload failed for creds/${rel}"
                _T2_OK=false
            fi
        done < <(find creds -type f -name '*.gpg' -print0 2>/dev/null)
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
            # Count COMPLETE snapshots present this run (grep -c . not `wc -l`,
            # which counts 1 for an empty var). _T2_PRUNED tracks successful
            # deletes; retained = total - pruned, surfaced in backup_status.json.
            _T2_PRUNED=0
            _T2_COMPLETE_TOTAL=$(printf '%s\n' "$_gfs_complete" | grep -c . || true)
            for _st in $_gfs_delete; do
                if [ "$_st" = "$_T2_STAMP" ]; then
                    continue   # never the current run's snapshot
                fi
                if backend_delete "$_T2_HOST_DIR/$_st"; then
                    _T2_PRUNED=$(( _T2_PRUNED + 1 ))
                    log "GFS prune: removed off-site snapshot $_st"
                else
                    log "WARNING: GFS prune failed for off-site snapshot $_st"
                fi
            done
            _T2_SNAPSHOT_COUNT=$(( _T2_COMPLETE_TOTAL - _T2_PRUNED ))
        fi
    fi
fi
backend_cleanup

# --- Ensure .gitignore excludes Tier 2 files ---
# Tier 1 (git): memory/, config_overrides/, secrets/, infrastructure/
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
    # Remote is in sync only if HEAD is not ahead of upstream — a PRIOR run's push
    # may have failed, leaving unpushed commits despite a clean worktree today.
    if git rev-list --count '@{u}..HEAD' 2>/dev/null | grep -qx 0; then
        _TIER1_PUSHED=true
    fi
else
    # Explicit error handling — set -e is suppressed by ||.
    # Without this, a corrupt git repo silently kills the script
    # (as happened 2026-05-08 through 2026-05-25: 17 days unnoticed).
    if git commit -m "backup: $(date -Iseconds)" --quiet 2>&1; then
        if ! git push --quiet 2>&1; then
            # Append, never overwrite — an earlier SF3/SF4 failure reason must
            # survive into the status file alongside the push failure.
            _FAILURE_REASON="${_FAILURE_REASON:+$_FAILURE_REASON; }git push failed — backup exists locally only (not replicated to remote)"
            log "ERROR: git push failed — backup exists locally only (not replicated to remote)"
        else
            _TIER1_PUSHED=true
            log "Backup committed and pushed"
        fi
    else
        _FAILURE_REASON="${_FAILURE_REASON:+$_FAILURE_REASON; }git commit failed (corrupt repo or index error)"
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
        # Escrow drift withheld the SQL dump from the off-site snapshot (the
        # target is fine — the escrowed passphrase is stale), so name the real
        # cause + fix instead of pointing at the off-site target.
        if [ "$_SQL_ESCROW_DRIFT" = true ]; then
            _send_telegram "⚠️ *Off-site DR degraded — backup passphrase escrow drift*

The local backup is OK (decrypts with the env passphrase), but the ESCROWED
passphrase a disaster-recovery box would use no longer matches — so the fresh
SQL dump was withheld from the off-site snapshot (last-good retained).
Re-escrow the current GENESIS_BACKUP_PASSPHRASE to restore off-site DR.
Time: $(date -Is)"
        else
            _send_telegram "⚠️ *Off-site replication failed*

The local backup is OK, but it was NOT replicated off-site.
Tier-2 status: ${_T2_STATUS}
Time: $(date -Is)
Off-site copies are missing — check the off-site target."
        fi
    fi
fi
log "Backup complete (success=$_SUCCESS)"
