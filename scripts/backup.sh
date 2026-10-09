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
#   GENESIS_BACKUP_EXTRA_DIRS  — ':'-separated directories under $HOME (`~/` ok) to
#                                keep as encrypted, off-site-only tar archives
#                                (§6f). Unset = none. Restored by restore.sh §4c.
#   GENESIS_BACKUP_EXTRA_EXCLUDES — ':'-separated names excluded at any depth from
#                                those archives, on top of the built-in rebuildable
#                                caches (.venv, node_modules, __pycache__, …).
set -euo pipefail
umask 077

# Resolve HOME when unset: stripped-env/systemd/sandbox invocations can leave
# HOME unset, which under `set -u` aborts at the first ${HOME} use. Fall back
# to the passwd entry for the current uid (same source Path.home() uses); fail
# closed if unresolvable. See CC memory sandbox_shell_no_home.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "ERROR: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

# Pluggable Tier-2 (off-site) backend interface — selects none/local/smb at runtime
# (backward-compat: a configured GENESIS_BACKUP_NAS with no selector → smb).
_SCRIPT_DIR="$(unset CDPATH; cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
_GENESIS_HOME="${GENESIS_HOME:-$HOME/.genesis}"
_STATUS_FILE="$_GENESIS_HOME/backup_status.json"
_RUN_ID="${GENESIS_BACKUP_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
_TRIGGER="${GENESIS_BACKUP_TRIGGER:-timer}"
dr_lock_open
if ! flock -n "$DR_LOCK_FD"; then
    _holder="$(cat "$DR_LOCK_FILE" 2>/dev/null || true)"
    if [ "$_TRIGGER" = "update" ]; then
        mkdir -p "$(dirname "$_STATUS_FILE")"
        _safe_holder=$(printf '%s' "${_holder:-unknown}" | sed 's/\\/\\\\/g; s/"/\\"/g; s/[[:cntrl:]]/ /g')
        cat > "$_STATUS_FILE" <<STATUSEOF
{"timestamp":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","run_id":"$_RUN_ID","success":false,"sqlite_lines":0,"failure_reason":"backup-restore lock held by $_safe_holder","failure_class":"db_integrity","failure_stage":"lock_busy","db_integrity_status":"indeterminate","sqlite_backup_verified":false,"tier2_status":"unknown","tier2_backend":"none","tier1_pushed":false}
STATUSEOF
        echo "[genesis-backup] $(date -Iseconds) FAILED: backup-restore lock held by ${_holder:-unknown} — update backup not run" >&2
        exit 73
    fi
    echo "[genesis-backup] $(date -Iseconds) SKIPPED: backup-restore lock held by ${_holder:-unknown} — backup not run"
    exit 0
fi
dr_lock_stamp backup

# ── Status tracking ──────────────────────────────────────────────────
_STARTED_AT=$(date +%s)
_SQLITE_LINES=0
_QDRANT_COUNT=0
_TRANSCRIPT_COUNT=0
_MEMORY_COUNT=0
_EXTRA_SKIP_LABELS=()  # declared before the EXIT trap can fire: its status write reads it
_EXTRA_PARTIAL_LABELS=()
# What this run's off-site alert block actually ANNOUNCED (or carried forward from a
# run that did). A run that dies before the alert block records nothing announced, so
# the next run alerts instead of assuming the earlier one did.
_ALERTED_CORE=false
_ALERTED_GAP=""
# A short fingerprint of WHICH listed directories this run left out (empty when none),
# so the off-site alert re-fires when a different directory goes missing.
_extras_gap_fingerprint() {
    declare -p _EXTRA_SKIP_LABELS _EXTRA_PARTIAL_LABELS >/dev/null 2>&1 || return 0
    [ $((${#_EXTRA_SKIP_LABELS[@]} + ${#_EXTRA_PARTIAL_LABELS[@]})) -gt 0 ] || return 0
    local _l
    {
        for _l in "${_EXTRA_SKIP_LABELS[@]+"${_EXTRA_SKIP_LABELS[@]}"}"; do printf 'skipped %s\n' "$_l"; done
        for _l in "${_EXTRA_PARTIAL_LABELS[@]+"${_EXTRA_PARTIAL_LABELS[@]}"}"; do printf 'partial %s\n' "$_l"; done
    } | LC_ALL=C sort -u | sha1sum | cut -c1-12
}
_SECRETS_OK=false
_SUCCESS=false
_FAILURE_REASON=""
_FAILURE_CLASS="none"
_FAILURE_STAGE=""
_DB_INTEGRITY_STATUS="indeterminate"
_SQLITE_BACKUP_VERIFIED=false
# SF3 freshness tracking: only payloads regenerated THIS run may enter the
# off-site dated snapshot — a leftover .gpg from a prior run must never be
# re-badged under a fresh COMPLETE stamp (it silently misrepresents recency,
# and GFS retention then ages out the snapshots holding genuinely-fresh data).
_SQL_FRESH=false        # SQL dump regenerated (encrypted) this run
_SQL_RESTORABLE=false   # AND round-trip-verified → eligible for off-site COMPLETE
_SQL_ESCROW_DRIFT=false # env-decryptable but escrow stale → off-site DR degraded
_QDRANT_FRESH=""    # space-separated collections snapshotted+encrypted this run
_QDRANT_FAILED=""   # collections that EXIST (HTTP 200) but failed to snapshot this run
_SQL_TMP=""         # plaintext ~269MB dump temp — trap-cleaned (N2, credential-bearing)
_SQL_ARTIFACT_TMP="" # encrypted candidate — promoted only after round-trip verification
_SQL_VERIFY_TMP=""   # decrypted candidate used for exact restore validation
_VERIFY_DB=""         # imported verification DB and sidecars — trap-cleaned
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
    # Separate signals so a chronic opt-in extras gap can never mask (or be masked
    # by) a real off-site failure in the alert dedup below.
    local _offsite_core_complete=false _extras_complete=true _extras_gap
    _extras_gap="$(_extras_gap_fingerprint)"
    if [ "${_T2_STATUS:-}" = "ok" ] || [ "${_T2_EXTRAS_ONLY_PARTIAL:-false}" = true ]; then
        _offsite_core_complete=true
    fi
    if [ $((${_EXTRA_SKIPPED:-0} + ${_EXTRA_UPLOAD_FAILED:-0} + ${_EXTRA_PARTIAL:-0})) -gt 0 ]; then _extras_complete=false; fi
    mkdir -p "$(dirname "$_STATUS_FILE")"
    cat > "$_STATUS_FILE" <<STATUSEOF
{"timestamp":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","run_id":"$_RUN_ID","success":$_SUCCESS,"sqlite_lines":$_SQLITE_LINES,"qdrant_collections":$_QDRANT_COUNT,"transcript_files":$_TRANSCRIPT_COUNT,"memory_files":$_MEMORY_COUNT,"eval_files":${_EVAL_COUNT:-0},"extra_dirs":${_EXTRA_COUNT:-0},"extra_dirs_skipped":${_EXTRA_SKIPPED:-0},"extra_dirs_partial":${_EXTRA_PARTIAL:-0},"extra_upload_failed":${_EXTRA_UPLOAD_FAILED:-0},"secrets_encrypted":$_SECRETS_OK,"duration_s":$_duration,"failure_reason":"$_safe_reason","failure_class":"$_FAILURE_CLASS","failure_stage":"$_FAILURE_STAGE","db_integrity_status":"$_DB_INTEGRITY_STATUS","sqlite_backup_verified":$_SQLITE_BACKUP_VERIFIED,"tier2_status":"${_T2_STATUS:-unknown}","offsite_confirmed":$_offsite_confirmed,"offsite_core_complete":$_offsite_core_complete,"extras_complete":$_extras_complete,"extras_gap":"$_extras_gap","offsite_core_alerted":${_ALERTED_CORE:-false},"extras_alerted_gap":"${_ALERTED_GAP:-}","tier2_backend":"${_T2_BACKEND:-none}","snapshot_id":"${_T2_STAMP:-}","snapshot_count":${_T2_SNAPSHOT_COUNT:-null},"pruned_count":${_T2_PRUNED:-null},"tier1_pushed":$_TIER1_PUSHED}
STATUSEOF
}

# BK-N1: fire the backup-FAILED Telegram alert from the EXIT trap, not inline at
# the end — so an abort OUTSIDE the handled git block (mktemp, git add, clone,
# an unexpected `set -e` death) still alerts instead of only writing
# success:false to the status file. Fires exactly once (the inline 🚨 block was
# removed); the off-site ⚠️ alert stays inline (only reachable on a completed
# run, and its dedup must read the prior status before _write_status rewrites it).
_alert_backup_failed() {
    [ "$_SUCCESS" = "true" ] && return 0
    # `set -e` stays active inside the EXIT trap, and this is the FIRST step of
    # _on_exit — so it must never abort _on_exit (which would skip _write_status
    # + the plaintext cleanup below). Two ways it could: (a) an abort in the
    # narrow window before _send_telegram is defined (a 0-byte secrets.env
    # failing load_secrets_file) → `declare -F` guard skips the call, and
    # _write_status still records success:false for the health-alert path;
    # (b) _send_telegram → queue_alert returning non-zero → `|| true`. Always
    # returns 0.
    if declare -F _send_telegram >/dev/null 2>&1; then
        _send_telegram "🚨 *Backup failed*

Reason: ${_FAILURE_REASON:-unknown (aborted before completion)}
Time: $(date -Is)
SQLite lines: $_SQLITE_LINES
Duration: $(( $(date +%s) - _STARTED_AT ))s" || true
    fi
    return 0
}

_on_exit() {
    # Exit handler: every step best-effort so a failure in one never skips the
    # rest (status write + plaintext cleanup must always run).
    _alert_backup_failed || true
    _write_status || true
    backend_cleanup || true
    # N2: the credential-bearing plaintext SQL dump must not outlive the script
    # if it died mid-section (before its inline rm).
    rm -f "${_SQL_TMP:-}" "${_SQL_ARTIFACT_TMP:-}" "${_SQL_VERIFY_TMP:-}" 2>/dev/null || true
    rm -f "${_EXTRA_TAR_TMP:-}" "${_EXTRA_TAR_ERR:-}" 2>/dev/null || true  # §6f plaintext tar + its file-name diagnostics
    if [ -n "${_VERIFY_DB:-}" ]; then
        rm -f "$_VERIFY_DB" "$_VERIFY_DB-journal" "$_VERIFY_DB-wal" "$_VERIFY_DB-shm" 2>/dev/null || true
    fi
    return 0
}
trap _on_exit EXIT

GENESIS_DIR="${GENESIS_DIR:-$HOME/genesis}"
BACKUP_DIR="$HOME/backups/genesis-backups"
# Derive CC project dir from genesis dir path (CC convention: / → -)
_CC_PROJECT_ID=$(echo "$GENESIS_DIR" | tr '/' '-')
MEMORY_DIR="$HOME/.claude/projects/${_CC_PROJECT_ID}/memory"
TRANSCRIPT_DIR="$HOME/.claude/projects/${_CC_PROJECT_ID}"
# shellcheck source=scripts/lib/backup_core_paths.sh
source "$_SCRIPT_DIR/lib/backup_core_paths.sh"
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
# inherited TMPDIR: for a CC-launched run that is ~/.genesis/cc-tmp (the quota-capped
# "oxygen" volume — filling it breaks every CC session's temp); for the 6h timer unit
# it is /tmp (often tmpfs/RAM).
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
if [ -f "$SECRETS_FILE" ] && ! backup_passphrase_file_valid "$SECRETS_FILE"; then
    die "Backup secrets file is unreadable or contains NUL"
fi
if [ -n "$_BACKUP_PASSPHRASE" ]; then
    backup_passphrase_valid "$_BACKUP_PASSPHRASE" || die "Backup passphrase must be a single line without a terminator"
    _ENCRYPT_READY=true
fi

# encrypt_file <src> <dst> — encrypt file contents to <dst> (e.g. *.gpg).
encrypt_file() {
    local src="$1"
    local dst="$2"
    backup_passphrase_valid "$_BACKUP_PASSPHRASE" || return 1
    printf '%s' "$_BACKUP_PASSPHRASE" | gpg --batch --yes --no-symkey-cache --passphrase-fd 0 \
        --symmetric --cipher-algo AES256 -o "$dst" "$src" 2>/dev/null
}

# Git network ops run while the backup↔restore lock is held for the whole run,
# so an unbounded stall (half-open TCP to the remote — kernel retransmit can
# hang 10-15min) would block a concurrent DR restore past its wait. Bound them
# (named failure: stalled push/pull/clone holding the DR lock). GENESIS-
# overridable for slow links; -k SIGKILLs a SIGTERM-ignoring git.
_GIT_NET_TIMEOUT="${GENESIS_BACKUP_GIT_TIMEOUT:-300}"
_git_net() { timeout -k 10 "$_GIT_NET_TIMEOUT" git "$@"; }

# _roundtrip_ok <passphrase> <artifact.gpg> [stderr_file] — prove that the exact
# encrypted candidate decrypts, imports into a new database, and passes full
# integrity, foreign-key, and non-empty-schema checks.  Checking only the live
# source before `.dump` leaves a race; checking only the dump's COMMIT trailer
# misses broken UNIQUE indexes and other logical inconsistencies.
_roundtrip_ok() {
    local _pass="$1" _art="$2" _errf="${3:-/dev/null}" _gpg_rc _sqlite_rc _ic _fk _schema
    backup_passphrase_valid "$_pass" || return 1
    _VERIFY_DB=$(mktemp -p "$GENESIS_BIG_TMP")
    rm -f "$_VERIFY_DB"
    _SQL_VERIFY_TMP=$(mktemp -p "$GENESIS_BIG_TMP")
    printf '%s' "$_pass" | python3 \
        "$_SCRIPT_DIR/../src/genesis/guardian/cred_integrity.py" decrypt-backup "$_art" "$_SQL_VERIFY_TMP" 2>"$_errf"
    _gpg_rc=${PIPESTATUS[1]}
    if [ "$_gpg_rc" -eq 0 ]; then
        sqlite3 "$_VERIFY_DB" ".bail on" ".read $_SQL_VERIFY_TMP" 2>>"$_errf"
        _sqlite_rc=$?
    else
        _sqlite_rc=1
    fi
    if [ "$_gpg_rc" -ne 0 ] || [ "$_sqlite_rc" -ne 0 ]; then
        rm -f "$_SQL_VERIFY_TMP" "$_VERIFY_DB" "$_VERIFY_DB-journal" "$_VERIFY_DB-wal" "$_VERIFY_DB-shm"
        _SQL_VERIFY_TMP=""
        _VERIFY_DB=""
        return 1
    fi
    _ic=$(sqlite3 "$_VERIFY_DB" "PRAGMA integrity_check;" 2>>"$_errf") || _ic=""
    _fk=$(sqlite3 "$_VERIFY_DB" "PRAGMA foreign_key_check;" 2>>"$_errf") || _fk="CHECK_FAILED"
    _schema=$(sqlite3 "$_VERIFY_DB" "SELECT count(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%';" 2>>"$_errf") || _schema=0
    rm -f "$_SQL_VERIFY_TMP" "$_VERIFY_DB" "$_VERIFY_DB-journal" "$_VERIFY_DB-wal" "$_VERIFY_DB-shm"
    _SQL_VERIFY_TMP=""
    _VERIFY_DB=""
    [ "$_ic" = "ok" ] && [ -z "$_fk" ] && [ "${_schema:-0}" -gt 0 ]
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
        ROUNDTRIP_VERDICT=RESTORABLE
        return 0
    fi
    # Primary failed. If the primary WAS escrow, an env-passphrase success means
    # drift (artifact good, escrow stale); env failure means genuine corruption.
    if $_have_escrow && _roundtrip_ok "$_BACKUP_PASSPHRASE" "$_art" /dev/null; then
        rm -f "$_errf"
        ROUNDTRIP_VERDICT=DRIFT
        return 0
    fi
    # Surface the gpg error, sanitized to printable ASCII: _write_status only
    # escapes quotes/backslashes, so a raw tab/CR/UTF-8 gpg message would make
    # backup_status.json invalid — and the health consumer's read_text()+
    # json.loads would then throw UnicodeDecodeError (uncaught → crashes health)
    # or JSONDecodeError (swallowed → suppresses the very CRITICAL this raises).
    ROUNDTRIP_DETAIL=$(LC_ALL=C tr -cd '[:print:]' < "$_errf" | cut -c1-200)
    rm -f "$_errf"
    ROUNDTRIP_VERDICT=CORRUPT
    return 0
}

# Refuse before touching the backup clone or any canonical artifact.  A dump
# can sometimes finish despite index corruption, but promoting it would erase
# the provenance boundary between a known-good backup and a damaged source.
DB_FILE="$GENESIS_DIR/data/genesis.db"
if [ ! -f "$DB_FILE" ]; then
    die "SQLite source database not found at $DB_FILE"
fi
_DB_CHECK_OUTPUT=""
_DB_CHECK_RC=0
if [ -x "$GENESIS_DIR/.venv/bin/python" ] \
    && [ -f "$GENESIS_DIR/src/genesis/db/integrity.py" ]; then
    _DB_CHECK_OUTPUT=$(PYTHONPATH="$GENESIS_DIR/src" \
        "$GENESIS_DIR/.venv/bin/python" -m genesis.db.integrity check \
        "$DB_FILE" --source backup --quarantine-on-failure 2>&1) \
        || _DB_CHECK_RC=$?
else
    _DB_INTEGRITY_STATUS="indeterminate"
    _FAILURE_CLASS="db_integrity"
    _FAILURE_STAGE="integrity_checker_unavailable"
    die "SQLite integrity checker unavailable — refusing to create an unverified backup"
fi
if [ "$_DB_CHECK_RC" -ne 0 ] || [ "$_DB_CHECK_OUTPUT" != "OK" ]; then
    _DB_CHECK_SAFE=$(printf '%s' "${_DB_CHECK_OUTPUT:-no output}" \
        | LC_ALL=C tr -cd '[:print:]\n' | tr '\n' ';' | cut -c1-500)
    _DB_INTEGRITY_STATUS="indeterminate"
    _FAILURE_CLASS="db_integrity"
    _FAILURE_STAGE="source_integrity"
    # A stable failure creates the durable marker. Contain every long-lived
    # writer only when that marker exists; an identity-race result is
    # indeterminate evidence and must not claim that a replacement was corrupt.
    if PYTHONPATH="$GENESIS_DIR/src" "$GENESIS_DIR/.venv/bin/python" -c '
import sys
from genesis.db.integrity import database_is_quarantined
raise SystemExit(0 if database_is_quarantined(sys.argv[1]) else 1)
' "$DB_FILE"; then
        _DB_INTEGRITY_STATUS="corrupt"
        for _svc in genesis-server.service genesis-bridge.service; do
            systemctl --user stop "$_svc" 2>/dev/null \
                || log "WARNING: could not stop $_svc after DB quarantine"
        done
    fi
    die "SQLite source integrity check failed — last-known-good SQL artifact and NAS snapshots preserved ($_DB_CHECK_SAFE)"
fi
_DB_INTEGRITY_STATUS="healthy"
log "SQLite source integrity check: ok"

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
            _SQL_ARTIFACT_TMP="data/.genesis.sql.gpg.partial.$$"
            if encrypt_file "$_SQL_TMP" "$_SQL_ARTIFACT_TMP"; then
                log "SQLite: $_SQLITE_LINES lines (encrypted)"
                # SF4 round-trip: classify the fresh artifact's restorability
                # (see _verify_sql_roundtrip). ROUNDTRIP_DETAIL is set (sanitized)
                # only on CORRUPT. _SQL_RESTORABLE gates the OFF-SITE upload so a
                # DR box never auto-selects a COMPLETE snapshot it can't decrypt.
                ROUNDTRIP_DETAIL=""
                ROUNDTRIP_VERDICT=""
                _verify_sql_roundtrip "$_SQL_ARTIFACT_TMP"
                case "$ROUNDTRIP_VERDICT" in
                    RESTORABLE)
                        mv "$_SQL_ARTIFACT_TMP" data/genesis.sql.gpg
                        _SQL_ARTIFACT_TMP=""
                        _SQL_FRESH=true
                        _SQL_RESTORABLE=true
                        _SQLITE_BACKUP_VERIFIED=true
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
                        mv "$_SQL_ARTIFACT_TMP" data/genesis.sql.gpg
                        _SQL_ARTIFACT_TMP=""
                        _SQL_FRESH=true
                        _SQL_RESTORABLE=false
                        _SQLITE_BACKUP_VERIFIED=true
                        _SQL_ESCROW_DRIFT=true
                        log "WARNING: SQL round-trip failed with the ESCROWED passphrase but SUCCEEDED with the env one — escrow is stale (secrets.env rotated?). Local backup OK; off-site DR degraded until re-escrow."
                        ;;
                    *)  # CORRUPT
                        _SQL_RESTORABLE=false
                        _FAILURE_CLASS="db_backup"
                        _FAILURE_STAGE="sqlite_roundtrip"
                        _FAILURE_REASON="${_FAILURE_REASON:+$_FAILURE_REASON; }SQL archive failed round-trip decrypt with its own env passphrase (${ROUNDTRIP_DETAIL:-no gpg output}) — corrupt/unrestorable, withheld from off-site"
                        log "WARNING: $_FAILURE_REASON"
                        _SQLITE_LINES=0
                        rm -f "$_SQL_ARTIFACT_TMP"
                        _SQL_ARTIFACT_TMP=""
                        ;;
                esac
            else
                _FAILURE_CLASS="db_backup"
                _FAILURE_STAGE="sqlite_encrypt"
                log "WARNING: SQLite encryption failed"
                _SQLITE_LINES=0
                rm -f "$_SQL_ARTIFACT_TMP"
                _SQL_ARTIFACT_TMP=""
            fi
        else
            _FAILURE_CLASS="db_backup"
            _FAILURE_STAGE="sqlite_dump"
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
    # curl -w already prints "000" on a connection failure AND exits non-zero,
    # so `|| echo 000` would double-append → "000000"; swallow the exit with
    # `|| true` and default only a genuinely-empty capture (curl absent).
    _probe_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
        "$QDRANT_URL/collections/$collection" 2>/dev/null || true)
    _probe_code=${_probe_code:-000}
    if [ "$_probe_code" = "404" ]; then
        log "Qdrant: collection $collection does not exist — skipping"
        continue
    elif [ "$_probe_code" != "200" ]; then
        # Server unreachable/erroring (000/timeout/5xx) — we CANNOT confirm the
        # collection exists. Qdrant is rebuildable from the verified SQL dump,
        # so this does not invalidate the SQLite artifact and an update may
        # continue. It is nevertheless an incomplete backup: persist and alert
        # it as a non-DB failure instead of reporting a false success.
        log "WARNING: Qdrant unreachable probing $collection (HTTP $_probe_code) — skipping (rebuildable from SQL)"
        _FAILURE_REASON="${_FAILURE_REASON:+$_FAILURE_REASON; }Qdrant unreachable probing $collection (HTTP $_probe_code)"
        if [ "$_FAILURE_CLASS" = "none" ]; then
            _FAILURE_CLASS="non_db"
            _FAILURE_STAGE="qdrant_probe"
        fi
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
    if [ "$_FAILURE_CLASS" = "none" ]; then
        _FAILURE_CLASS="non_db"
        _FAILURE_STAGE="qdrant_snapshot"
    fi
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

# --- 6f. Opt-in extra directories (encrypted, Tier 2 / off-site only) ---
# GENESIS_BACKUP_EXTRA_DIRS lists ':'-separated directories UNDER $HOME (a leading
# `~/` is expanded) that this install wants kept — install-local data no other
# section knows about. Each becomes ONE encrypted tar, extra/<name>.tar.gpg, so a
# nested tree survives the single-level off-site listing restore.sh reads back.
# Members are stored relative to $HOME and restore writes them back there.
# extra/ is gitignored like transcripts/: off-site only, never the git tier.
# Rebuildable caches are excluded at any depth (MEASURED, GNU tar 1.35:
# `--exclude=.venv` drops a/.venv and a/sub/.venv but keeps a/keep.venvx), plus
# GENESIS_BACKUP_EXTRA_EXCLUDES (tar patterns, wildcards allowed).
# `--exclude-vcs-ignores` is NOT used: measured on the same tar, it kept a `.venv/`
# the directory's .gitignore excluded.
# FRESH ONLY: extra/ is emptied at the start of every run and holds only what this
# run archived, so the snapshot never carries an old archive under a new date and
# nothing ever needs pruning. A listed directory that is not archived this run
# (refused, missing, unreadable, emptied by an exclude) is simply absent from this
# snapshot, counted in extra_dirs_skipped, and marks the off-site copy partial;
# older snapshots keep it until retention drops them. It never fails the backup.
_EXTRA_COUNT=0
_EXTRA_SKIPPED=0       # listed directories NOT archived this run, for any reason
_EXTRA_TAR_TMP=""
_EXTRA_TAR_ERR=""
_EXTRA_UPLOAD_FAILED=0
_EXTRA_UPLOADED=()     # names that reached the off-site snapshot (recorded in COMPLETE)
_EXTRA_BUILT=()        # names archived THIS run: the only ones uploaded
_EXTRA_SKIP_LABELS=()  # listed entries not archived this run (recorded in COMPLETE)
_EXTRA_PARTIAL=0       # archived, but a restore will refuse some of their members
_EXTRA_PARTIAL_LABELS=()
_extra_prev=0
if mkdir -p extra 2>/dev/null; then
    _extra_prev="$(find extra -maxdepth 1 -type f -name '*.tar.gpg' 2>/dev/null | wc -l || true)"
    find extra -maxdepth 1 -type f -delete 2>/dev/null || true
    # A file cleanup could not remove is never restored: restore takes only the
    # names .extra-manifest lists, which is rewritten below on every run. It sits
    # outside extra/, so an extra/ that cannot be written cannot pin an old one.
    if find extra -maxdepth 1 -type f -print -quit 2>/dev/null | grep -q .; then
        log "WARNING: extra/ still holds files from an earlier run that could not be removed; restore ignores them (only .extra-manifest's names are restored)"
    fi
else
    log "WARNING: cannot create $BACKUP_DIR/extra; extra directories have no local copy this run"
fi
_extra_skip() {  # <reason> <entry>
    log "WARNING: extra dir skipped ($1): $2"
    _EXTRA_SKIPPED=$((_EXTRA_SKIPPED + 1))
    # Shown $HOME-relative in the COMPLETE marker, so restore can say which listed
    # directory a snapshot does not hold. Shell-quoted under the C locale, so every
    # byte becomes printable ASCII and no name ever shrinks to an empty label.
    _EXTRA_SKIP_LABELS+=("$(LC_ALL=C printf '%q' "${2#"$HOME"/}" | cut -c1-200)")
}
if [ -z "${GENESIS_BACKUP_EXTRA_DIRS:-}" ]; then
    [ "$_extra_prev" -gt 0 ] && log "NOTE: GENESIS_BACKUP_EXTRA_DIRS is unset; the previous run's $_extra_prev extra archive(s) are not carried forward (older off-site snapshots keep them until retention)"
elif [[ "$GENESIS_BACKUP_EXTRA_DIRS" == *$'\n'* ]] || [[ "${GENESIS_BACKUP_EXTRA_EXCLUDES:-}" == *$'\n'* ]]; then
    _extra_skip "the list contains a newline; entries are ':'-separated" "GENESIS_BACKUP_EXTRA_DIRS / GENESIS_BACKUP_EXTRA_EXCLUDES"
else
    # Compare RESOLVED paths: a symlinked or slash-terminated $HOME must not reject every entry.
    _home_real="$(realpath -- "$HOME")"
    _home_lex="$(realpath -s -m -- "$HOME")"
    _bdir_real="$(realpath -m -- "$BACKUP_DIR")"
    _btmp_real="$(realpath -m -- "$GENESIS_BIG_TMP")"
    _lroot_real=""
    if [ "$(_backend_resolve)" = local ] && [ -n "${GENESIS_BACKUP_LOCAL_PATH:-}" ]; then
        _lroot_real="$(realpath -m -- "$GENESIS_BACKUP_LOCAL_PATH")"
    fi
    # Class 3 — an archive is only worth keeping if a restore can extract it. The
    # restore helper refuses a Python whose tarfile lacks the 2025 extraction-filter
    # fixes, so find that out NOW (loudly, as a partial backup), not during a disaster
    # recovery. (The restore box needs a fixed Python too; SETUP.md says so.)
    _extra_py_why=""
    if ! _extra_py_why="$(python3 "$_SCRIPT_DIR/lib/extra_restore.py" check 2>&1)"; then
        _extra_py_why="${_extra_py_why:-the restore helper could not run}"
    else
        _extra_py_why=""
    fi
    _EXTRA_DEFAULT_EXCLUDES=(.venv venv node_modules __pycache__ .pytest_cache .ruff_cache .mypy_cache .tox)
    _extra_ex_args=()
    for _x in "${_EXTRA_DEFAULT_EXCLUDES[@]}"; do _extra_ex_args+=("--exclude=$_x"); done
    IFS=':' read -r -a _extra_user_ex <<< "${GENESIS_BACKUP_EXTRA_EXCLUDES:-}"
    for _x in "${_extra_user_ex[@]+"${_extra_user_ex[@]}"}"; do
        [ -n "$_x" ] && _extra_ex_args+=("--exclude=$_x")
    done
    _extra_abs_seen=()
    _extra_listed=0
    IFS=':' read -r -a _extra_dirs <<< "$GENESIS_BACKUP_EXTRA_DIRS"
    for _d in "${_extra_dirs[@]+"${_extra_dirs[@]}"}"; do
        [ -n "$_d" ] || continue
        _extra_listed=$((_extra_listed + 1))
        # The quoted "~" patterns match the literal `~` TEXT a config value carries,
        # which is then expanded here by hand — not a tilde the shell should expand.
        # shellcheck disable=SC2088
        case "$_d" in
            "~") _d="$HOME" ;;
            "~/"*) _d="$HOME/${_d#\~/}" ;;
        esac
        case "$_d" in
            /*) ;;
            *)
                # A relative entry would resolve against this script's cwd (the backups repo).
                _extra_skip "must be an absolute path or start with ~/" "$_d"
                continue
                ;;
        esac
        _abs="$(realpath -m -- "$_d")"
        case "$_abs" in
            "$_home_real"/?*) ;;
            *)
                _extra_skip "must be a directory under \$HOME, not \$HOME itself" "$_d"
                continue
                ;;
        esac
        _rel="${_abs#"$_home_real"/}"
        # A symlinked entry (or one under a symlinked dir) would be archived under its
        # target's path, and restore would never recreate the path that was listed.
        _lex="$(realpath -s -m -- "$_d")"
        _rel_lex="${_lex#"$_home_lex"/}"
        [ "$_rel_lex" = "$_lex" ] && _rel_lex="${_lex#"$_home_real"/}"
        if [ "$_rel_lex" != "$_rel" ]; then
            _extra_skip "it is, or runs through, a symlink; list the real directory ~/$_rel" "$_d"
            continue
        fi
        _overlap=""
        for _guard in "$_bdir_real" "$_btmp_real" "$_lroot_real"; do
            [ -n "$_guard" ] || continue
            case "$_abs/" in "$_guard"/*) _overlap="$_guard" ;; esac
            case "$_guard/" in "$_abs"/*) _overlap="$_guard" ;; esac
        done
        if [ -n "$_overlap" ]; then
            # Archiving the backups repo, the backup temp dir or a local off-site root
            # (or anything containing one) would archive the backup's own output.
            _extra_skip "same as, inside, or containing the backup's own output $_overlap" "$_d"
            continue
        fi
        if _core_hit="$(backup_core_overlap "$_abs")"; then
            # restore.sh puts an extra directory back as a unit, which would swap this
            # core path out of the way; the core backup already covers it.
            _extra_skip "overlaps $_core_hit, which the core backup restores" "$_d"
            continue
        else
            _core_rc=$?
            if [ "$_core_rc" -ne 1 ]; then
                _extra_skip "core path separation could not be established" "$_d"
                continue
            fi
        fi
        _dup=""
        for _prev in "${_extra_abs_seen[@]+"${_extra_abs_seen[@]}"}"; do
            case "$_abs/" in "$_prev"/*) _dup="$_prev" ;; esac
            case "$_prev/" in "$_abs"/*) _dup="$_prev" ;; esac
        done
        if [ -n "$_dup" ]; then
            _extra_skip "same as, inside, or containing another listed entry ~/${_dup#"$_home_real"/}" "$_d"
            continue
        fi
        if [ ! -d "$_abs" ]; then
            _extra_skip "missing; not in this snapshot" "$_d"
            continue
        fi
        # Recorded only for directories that exist, so a missing nested entry can
        # never get its existing parent refused.
        _extra_abs_seen+=("$_abs")
        _ex_hit=""
        IFS='/' read -r -a _rel_parts <<< "$_rel"
        for _part in "${_rel_parts[@]}"; do
            for _x in "${_extra_ex_args[@]}"; do [ "--exclude=$_part" = "$_x" ] && _ex_hit="$_part"; done
        done
        if [ -n "$_ex_hit" ]; then
            _extra_skip "its path contains the excluded name '$_ex_hit'; the archive would be empty" "$_d"
            continue
        fi
        if ! $_ENCRYPT_READY; then
            _extra_skip "GENESIS_BACKUP_PASSPHRASE not set — refusing plaintext" "$_d"
            continue
        fi
        if [ -n "$_extra_py_why" ]; then
            _extra_skip "a restore could not extract it: $_extra_py_why" "$_d"
            continue
        fi
        _name="$(printf '%s' "$_rel" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-80)-$(printf '%s' "$_rel" | sha1sum | cut -c1-8).tar.gpg"
        # Temp creation can fail (a full or inode-exhausted temp filesystem): skip this
        # directory, never abort the whole backup over an optional payload.
        if ! _tar_tmp="$(mktemp -p "$GENESIS_BIG_TMP" extra.XXXXXX.tar 2>/dev/null)"; then
            _extra_skip "cannot create a temp file in $GENESIS_BIG_TMP" "$_d"
            continue
        fi
        _EXTRA_TAR_TMP="$_tar_tmp"
        if ! _tar_err="$(mktemp -p "$GENESIS_BIG_TMP" extra.XXXXXX.err 2>/dev/null)"; then
            rm -f "$_tar_tmp"
            _EXTRA_TAR_TMP=""
            _extra_skip "cannot create a temp file in $GENESIS_BIG_TMP" "$_d"
            continue
        fi
        _EXTRA_TAR_ERR="$_tar_err"
        _gpg_tmp="extra/.${_name}.partial.$$"
        _tar_rc=0
        # --hard-dereference: a hard link is stored as file content, so restore never
        # meets a hard-link member. Symlinks are kept as links; restore accepts the
        # ones that stay inside the restored directory.
        tar -C "$_home_real" --hard-dereference "${_extra_ex_args[@]}" -cf "$_tar_tmp" -- "$_rel" 2>"$_tar_err" || _tar_rc=$?
        # GNU tar exit 1 = some files changed while being read: the archive is written,
        # but a file that was changing may be torn. 2+ = fatal.
        _tar_head="$(head -3 "$_tar_err" | tr '\n' ' ')"
        rm -f "$_tar_err"
        _EXTRA_TAR_ERR=""
        if [ "$_tar_rc" -ge 2 ]; then
            rm -f "$_tar_tmp"
            _EXTRA_TAR_TMP=""
            _extra_skip "tar failed rc=$_tar_rc: $_tar_head" "$_d"
            continue
        fi
        [ "$_tar_rc" -eq 1 ] && log "NOTE: ~/$_rel changed while archiving; a file that was changing may be torn: $_tar_head"
        # tar treats excludes as wildcards, so a pattern can match the directory itself
        # and yield an archive without it, with rc 0. Check with the reader and the
        # member filter restore uses (never by parsing `tar -t`, which escapes
        # backslashes and, under a C locale, non-ASCII names), so backup keeps exactly
        # what restore will accept.
        _tar_root="" _tar_root_why="" _tar_verify_rc=0
        if _tar_root_err="$(mktemp -p "$GENESIS_BIG_TMP" extra.XXXXXX.err 2>/dev/null)"; then
            _EXTRA_TAR_ERR="$_tar_root_err"
            _tar_root="$(python3 "$_SCRIPT_DIR/lib/extra_restore.py" verify "$_tar_tmp" "$GENESIS_BIG_TMP" 2>"$_tar_root_err")" || _tar_verify_rc=$?
            _tar_root_why="$(head -c 300 "$_tar_root_err" | tr '\n' ' ')"
            rm -f "$_tar_root_err"
            _EXTRA_TAR_ERR=""
        else
            _tar_verify_rc=5
            _tar_root_why="cannot create a temp file in $GENESIS_BIG_TMP"
        fi
        _tar_partial=false
        if [ "$_tar_verify_rc" -eq 4 ] && [ "$_tar_root" = "$_rel" ]; then
            # Kept: everything else in the directory still restores. Recorded as
            # partial, so the off-site copy is not reported complete and the alert fires.
            _tar_partial=true
        elif [ "$_tar_verify_rc" -eq 0 ] && [ -n "$_tar_root" ] && [ "$_tar_root" != "$_rel" ]; then
            rm -f "$_tar_tmp"
            _EXTRA_TAR_TMP=""
            _extra_skip "the archive holds ${_tar_root} instead; does an exclude pattern match the directory itself?" "$_d"
            continue
        elif [ "$_tar_verify_rc" -ne 0 ] || [ "$_tar_root" != "$_rel" ]; then
            rm -f "$_tar_tmp"
            _EXTRA_TAR_TMP=""
            _extra_skip "restore could not use the archive (rc=$_tar_verify_rc: ${_tar_root_why:-no directory member}); does an exclude pattern match the directory itself?" "$_d"
            continue
        fi
        if encrypt_file "$_tar_tmp" "$_gpg_tmp" && mv -f "$_gpg_tmp" "extra/$_name"; then
            _EXTRA_COUNT=$((_EXTRA_COUNT + 1))
            _EXTRA_BUILT+=("$_name")
            if $_tar_partial; then
                log "WARNING: extra dir archived without members a restore would refuse (exclude them with GENESIS_BACKUP_EXTRA_EXCLUDES): $_d: $_tar_root_why"
                _EXTRA_PARTIAL=$((_EXTRA_PARTIAL + 1))
                _EXTRA_PARTIAL_LABELS+=("$(LC_ALL=C printf '%q' "$_rel" | cut -c1-200)")
            fi
        else
            rm -f "$_gpg_tmp"
            _extra_skip "encryption failed" "$_d"
        fi
        rm -f "$_tar_tmp"
        _EXTRA_TAR_TMP=""
    done
    if [ "$_extra_listed" -eq 0 ]; then
        _extra_skip "the list has no entries" "GENESIS_BACKUP_EXTRA_DIRS=$GENESIS_BACKUP_EXTRA_DIRS"
    fi
    log "Extra dirs: $_EXTRA_COUNT archived ($_EXTRA_PARTIAL partial), $_EXTRA_SKIPPED skipped"
fi
# Local manifest (same format as the off-site COMPLETE marker), written on EVERY run,
# the setting unset included: a restore without an off-site pull restores only the
# names it lists and reports what it says was skipped. Outside extra/ (gitignored
# below), written to a temp name and renamed, so a reader never sees half of it; on
# failure none is left behind, and a restore then restores no local extra archive
# rather than a stale one.
if {
    printf 'genesis-snapshot 1\n'
    for _n in "${_EXTRA_BUILT[@]+"${_EXTRA_BUILT[@]}"}"; do printf 'extra %s\n' "$_n"; done
    for _n in "${_EXTRA_SKIP_LABELS[@]+"${_EXTRA_SKIP_LABELS[@]}"}"; do printf 'skipped %s\n' "$_n"; done
    for _n in "${_EXTRA_PARTIAL_LABELS[@]+"${_EXTRA_PARTIAL_LABELS[@]}"}"; do printf 'partial %s\n' "$_n"; done
} 2>/dev/null > .extra-manifest.tmp && mv -f .extra-manifest.tmp .extra-manifest 2>/dev/null; then
    :
else
    rm -f .extra-manifest.tmp .extra-manifest 2>/dev/null || true
    log "WARNING: could not write .extra-manifest; a restore from this checkout will not restore its extra archives (the off-site snapshot is unaffected)"
fi

# --- 6d. Hook audit stores (Tier 1) ---
# The merge gate's override records: which merges bypassed which gate, and on what
# stated grounds. One small file per flush, own-user-only, and SELF-CONTAINED —
# every field (sigil, waived gate, PR, repo, head sha) means the same thing on a
# restored install, so the trail survives a rebuild intact. Plain copy, like the
# infrastructure profile above: the backups repo is private, and the writer
# refuses to persist command text, so these rows carry no credential material.
#
# The git-discard store is DELIBERATELY not here, and that is the whole reason this
# block names one store instead of globbing ~/.genesis. Its recovery payload is a
# `git stash create` sha that exists ONLY in the local repo's object store: such
# objects are never pushed, git prunes unreachable ones (default two weeks), and
# this script captures no object store at all. Restoring it onto a rebuilt
# container yields a list of pointers to nothing — a trail that LOOKS recoverable
# while every recovery attempt fails, which is worse than an absent one. At a live
# install, of 76 recorded shas one was already unresolvable in the repo that wrote
# it. Making it genuinely restorable means backing up the objects, not the records.
# ASK the writer where the store is; do not assume the default. An install that
# sets GENESIS_MERGE_OVERRIDE_DIR to a supported absolute path had its audit trail
# silently excluded here and restored to the wrong place — the trail lost on
# exactly the rebuild it exists for (Codex P2, PR #1609). One resolver, five
# consumers: see audit_jsonl.resolve_store_dir.
_OVERRIDE_STORE="$(python3 "$_SCRIPT_DIR/hooks/audit_jsonl.py" --store-dir GENESIS_MERGE_OVERRIDE_DIR 2>/dev/null \
    || printf '%s' "$HOME/.genesis/merge_overrides")"
if [ -d "$_OVERRIDE_STORE" ]; then
    log "Backing up hook audit stores..."
    mkdir -p audit/merge_overrides
    _AUDIT_COUNT=0
    _AUDIT_LIVE=""
    # LIST FIRST, and keep the listing's exit status. `find … 2>/dev/null` inside a
    # process substitution throws both away, so a store that cannot be listed — a
    # mode change on the directory, an I/O error — yielded an EMPTY `_AUDIT_LIVE`,
    # indistinguishable from a store with no records. The mirror loop below then
    # read every mirrored record as "gone from the live store" and deleted the lot:
    # the last known-good copies, destroyed by the very loop whose comment says that
    # must not happen (CodeRabbit Major, PR #1609).
    #
    # This is the SAME generator as the per-file bug above — deriving state from
    # whether work succeeded — one level up, at the listing instead of the copy.
    # Enumerated the other four `find`s in this store's paths while fixing it; each
    # of them fails toward doing LESS (no delete, no sweep, no restore, and
    # `_backup_has_payload` returning 1 aborts), so this was the only destructive one.
    _AUDIT_LIST="$(mktemp -p "$GENESIS_BIG_TMP")"
    _AUDIT_LISTED=true
    find "$_OVERRIDE_STORE" -maxdepth 1 -type f -name '*.jsonl' -print0 \
        > "$_AUDIT_LIST" 2>/dev/null || _AUDIT_LISTED=false
    while IFS= read -r -d '' _f; do
        _base="$(basename "$_f")"
        # The deletion set below is derived from THIS list — every name the live
        # store holds — never from which copies happened to succeed. Deriving it
        # from copy success meant a failed copy (a full backup filesystem is the
        # realistic one) marked the record absent, so the mirror loop then deleted
        # the last known-good copy of a record the local pruner may drop next
        # (Codex P2, PR #1609). Copy failure must cost at most a stale mirror
        # entry, never the entry.
        _AUDIT_LIVE="$_AUDIT_LIVE$_base"$'\n'
        # Stage and rename. A bare `cp` onto an existing destination TRUNCATES it
        # before it can fail, so a mid-copy failure destroys the previous good
        # mirror in place. rename(2) is atomic within the directory, so the
        # destination is either the old copy or the new one — never a partial.
        _tmp="audit/merge_overrides/.$_base.partial.$$"
        # cp's own stderr is CAPTURED, not discarded: it carries the only statement
        # of WHY (ENOSPC vs EACCES look identical without it), and the warning below
        # otherwise names the mitigation and never the cause.
        _AUDIT_ERR=""
        if _AUDIT_ERR="$(cp "$_f" "$_tmp" 2>&1)" \
            && _AUDIT_ERR="$(mv -f "$_tmp" "audit/merge_overrides/$_base" 2>&1)"; then
            _AUDIT_COUNT=$(( _AUDIT_COUNT + 1 ))
        else
            rm -f "$_tmp" 2>/dev/null || true
            log "WARNING: failed to copy $_base (${_AUDIT_ERR:-no error text}) — previous mirror copy, if any, left intact"
        fi
    done < "$_AUDIT_LIST"
    rm -f "$_AUDIT_LIST" 2>/dev/null || true
    # MIRROR the store, do not merely add to it. A copy-only loop left every
    # record the daily pruner had deleted in the backup forever: the mirror grows
    # past the 5 MB bound the store advertises, and a disaster restore
    # REINTRODUCES every record retention removed (Codex P2, PR #1609). Deleting
    # only names the live store no longer has keeps the backup a snapshot of the
    # store rather than its union over time.
    # ONLY when the live store was actually listed. Without that guard this loop
    # cannot tell "no records" from "could not look", and the two call for opposite
    # actions: delete everything, or touch nothing.
    _AUDIT_DROPPED=0
    if ! $_AUDIT_LISTED; then
        log "WARNING: could not list $_OVERRIDE_STORE — mirror prune SKIPPED (existing backup copies kept)"
    else
        # Reconcile the two name sets in ONE pass. This used to run a fresh `grep`
        # for every mirrored file, each rescanning the whole live-name string —
        # quadratic in a store that is one file per flush, and the advertised 5 MB
        # bound still holds tens of thousands of these small records. The cost
        # arrived precisely at the boundary the store is supposed to support, and
        # it delays the scheduled backup there (Codex P2, PR #1609).
        declare -A _AUDIT_LIVE_SET=()
        while IFS= read -r _ln; do
            [ -n "$_ln" ] && _AUDIT_LIVE_SET["$_ln"]=1
        done <<< "$_AUDIT_LIVE"
        while IFS= read -r -d '' _b; do
            _bbase="$(basename "$_b")"
            if [ -z "${_AUDIT_LIVE_SET[$_bbase]:-}" ]; then
                rm -f "$_b" && _AUDIT_DROPPED=$(( _AUDIT_DROPPED + 1 ))
            fi
        done < <(find audit/merge_overrides -maxdepth 1 -type f -name '*.jsonl' -print0 2>/dev/null)
        unset _AUDIT_LIVE_SET
    fi
    # Sweep any staging scrap a KILLED EARLIER run left behind, so the mirror cannot
    # grow a second, invisible store beside itself. Dot-prefixed, so the `*.jsonl`
    # loop above never sees one and never mistakes one for a record.
    #
    # Scoped the way the write is scoped, plus an age floor. A manual run overlapping
    # the 6h timer would otherwise delete the OTHER run's staged copy between its
    # `cp` and its `mv`, turning a good copy into a spurious warning. Ours are
    # already removed on the failure path above, so excluding `$$` costs nothing.
    find audit/merge_overrides -maxdepth 1 -type f -name '.*.partial.*' \
        ! -name "*.partial.$$" -mmin +60 -delete 2>/dev/null || true
    log "Hook audit stores: $_AUDIT_COUNT file(s), $_AUDIT_DROPPED pruned from mirror"
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
        "$HOME/.claude/settings.json:claude_settings.json" \
        "$HOME/.genesis/guardian_remote.yaml:guardian_remote.yaml" \
        "$HOME/.genesis/config/genesis.yaml:genesis.yaml" \
        "$HOME/.genesis/release-fingerprints.txt:release-fingerprints.txt"; do
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

    # Upload opt-in extra-dir archives (§6f). Flat by construction — one file
    # per directory — so restore's single-level off-site pull reads them back.
    # Only the archives THIS run built (never whatever else sits in extra/), so a
    # stale file can never reach a new snapshot under a new date.
    if [ "${#_EXTRA_BUILT[@]}" -gt 0 ]; then
        backend_mkdir "${_T2_DIR}/extra" || true  # a failed put below is counted per archive
        for fname in "${_EXTRA_BUILT[@]}"; do
            f="extra/$fname"
            if backend_put "$f" "${_T2_DIR}/extra/${fname}"; then
                _EXTRA_UPLOADED+=("$fname")
                log "  off-site: uploaded extra/${fname}"
            else
                # Opt-in payload: it never withholds the core snapshot's COMPLETE marker
                # (restore would then skip the core data too), but it does mark the off-site
                # copy unconfirmed below (tier2_status=partial, offsite_confirmed=false).
                log "WARNING: off-site upload failed for extra/${fname} (core snapshot still COMPLETE)"
                # Named in COMPLETE (written after this loop) so restore reports the gap.
                _EXTRA_SKIP_LABELS+=("${fname} (upload failed)")
                _EXTRA_UPLOAD_FAILED=$((_EXTRA_UPLOAD_FAILED + 1))
            fi
        done
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
        _T2_MARKER=$(mktemp)  # uploaded only after a full snapshot
        # The marker lists the extra archives this snapshot holds. restore.sh reads it
        # back to know what to expect, because a failed off-site LISTING looks the same
        # as an empty one, while a failed download of this file is detectable.
        {
            printf 'genesis-snapshot 1\n'
            for _n in "${_EXTRA_UPLOADED[@]+"${_EXTRA_UPLOADED[@]}"}"; do
                printf 'extra %s\n' "$_n"
            done
            for _n in "${_EXTRA_SKIP_LABELS[@]+"${_EXTRA_SKIP_LABELS[@]}"}"; do
                printf 'skipped %s\n' "$_n"
            done
            for _n in "${_EXTRA_PARTIAL_LABELS[@]+"${_EXTRA_PARTIAL_LABELS[@]}"}"; do
                printf 'partial %s\n' "$_n"
            done
        } > "$_T2_MARKER"
        if ! backend_put "$_T2_MARKER" "${_T2_DIR}/COMPLETE"; then
            log "WARNING: off-site upload failed for COMPLETE marker — snapshot unusable for restore"
            _T2_OK=false
        fi
        rm -f "$_T2_MARKER"
    fi

    if [ "$_T2_OK" = true ] && [ $((_EXTRA_UPLOAD_FAILED + _EXTRA_SKIPPED + _EXTRA_PARTIAL)) -gt 0 ]; then
        # Core snapshot is COMPLETE and restorable, but the off-site copy is not the
        # full set the operator asked for (an extra dir was not archived this run, or
        # its archive did not upload), so it is not reported as confirmed.
        _T2_STATUS="partial"
        _T2_EXTRAS_ONLY_PARTIAL=true  # core is complete: retention still runs (below)
        log "WARNING: Tier 2 snapshot ${_T2_STAMP} is COMPLETE but extra dirs are incomplete (${_EXTRA_SKIPPED} not archived, ${_EXTRA_PARTIAL} partial, ${_EXTRA_UPLOAD_FAILED} not uploaded)"
    elif [ "$_T2_OK" = true ]; then
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
    # is never pruned here. Runs only after a fully-uploaded (ok) snapshot this run, or
    # one whose core is COMPLETE and only opt-in extra dirs are missing: otherwise a
    # listed directory that stays missing would stop retention for good.
    if { [ "${_T2_STATUS:-}" = "ok" ] || [ "${_T2_EXTRAS_ONLY_PARTIAL:-false}" = true ]; } && backend_available; then
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
# Tier 1 (git): memory/, config_overrides/, secrets/, infrastructure/, audit/
# Tier 2 (off-site): data/, transcripts/, extra/
if ! grep -q '^data/$' .gitignore 2>/dev/null; then
    cat >> .gitignore << 'GITIGNORE'
# Tier 2 files — backed up off-site, not GitHub
data/
transcripts/
GITIGNORE
    log "Added Tier 2 exclusions to .gitignore"
fi
# extra/ (§6f) joined Tier 2 after the block above shipped: an existing install
# already has that block, so it would never receive this line from it.
if ! grep -qx 'extra/' .gitignore 2>/dev/null; then
    # Keep the appended line on its own even if the file lacks a final newline.
    if [ -s .gitignore ] && [ -n "$(tail -c1 .gitignore)" ]; then printf '\n' >> .gitignore; fi
    printf 'extra/\n' >> .gitignore
    log "Added extra/ to the Tier 2 .gitignore exclusions"
fi
if ! grep -qx '.extra-manifest' .gitignore 2>/dev/null; then
    if [ -s .gitignore ] && [ -n "$(tail -c1 .gitignore)" ]; then printf '\n' >> .gitignore; fi
    printf '.extra-manifest\n.extra-manifest.tmp\n' >> .gitignore
    log "Added .extra-manifest to the .gitignore exclusions"
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
            if [ "$_FAILURE_CLASS" = "none" ]; then
                _FAILURE_CLASS="non_db"
                _FAILURE_STAGE="git_push"
            fi
            log "ERROR: git push failed — backup exists locally only (not replicated to remote)"
        else
            _TIER1_PUSHED=true
            log "Backup committed and pushed"
        fi
    else
        _FAILURE_REASON="${_FAILURE_REASON:+$_FAILURE_REASON; }git commit failed (corrupt repo or index error)"
        if [ "$_FAILURE_CLASS" = "none" ]; then
            _FAILURE_CLASS="non_db"
            _FAILURE_STAGE="git_commit"
        fi
        log "ERROR: git commit failed — repository may need re-clone from remote"
    fi
fi

if [ "$_SQLITE_LINES" -gt 0 ] && [ "$_SQLITE_BACKUP_VERIFIED" = "true" ] \
    && [ -z "$_FAILURE_REASON" ]; then
    _SUCCESS=true
else
    if [ -z "$_FAILURE_REASON" ]; then
        _FAILURE_REASON="No SQLite data backed up"
    fi
    if [ "$_SQLITE_BACKUP_VERIFIED" != "true" ] && [ "$_FAILURE_CLASS" = "none" ]; then
        _FAILURE_CLASS="db_backup"
        _FAILURE_STAGE="sqlite_not_verified"
    fi
    log "WARNING: Backup incomplete — marking as failure (reason: $_FAILURE_REASON)"
fi

# The backup-FAILED alert now fires from the EXIT trap (_alert_backup_failed),
# so it covers early aborts too (BK-N1) and fires exactly once here.

# --- Alert on off-site replication failure (local backup still OK) ---
# Distinct from a backup failure: the local + Tier-1 backup succeeded, but the
# off-site copy did not fully land. Local-only installs (no off-site backend) are
# a valid choice and do NOT alert.
if [ "${_T2_BACKEND:-none}" != "none" ] && [ "$_T2_STATUS" != "ok" ]; then
    # Dedup: alert only on the transition INTO a failure. The status file still
    # holds the PREVIOUS run's state at this point (the EXIT trap rewrites it after).
    # This avoids a 6-hourly alert while the off-site target stays down; it re-alerts
    # once that signal recovers and then fails again. The core copy and the opt-in
    # extras are deduplicated SEPARATELY: one shared flag let a chronic extras gap
    # (a listed directory that stays missing) silence a later real off-site failure.
    # Older status files have no offsite_core_complete; offsite_confirmed stands in.
    # Each signal is suppressed only when the previous run ANNOUNCED the same thing
    # (offsite_core_alerted / extras_alerted_gap). Status files from before those
    # fields fall back to the older inference.
    _prev_flags=$(python3 -c "
import json
s = json.load(open('$_STATUS_FILE'))
core_was_down = s.get('offsite_core_complete', s.get('offsite_confirmed', True)) is False
core = s.get('offsite_core_alerted', core_was_down)
gap = s.get('extras_alerted_gap')
if gap is None:
    gap = s.get('extras_gap') if s.get('extras_complete', True) is False and not core_was_down else ''
print(core, gap or '-')
" 2>/dev/null || echo "False -")
    read -r _prev_core_alerted _prev_alerted_gap <<<"$_prev_flags"
    if [ "${_T2_EXTRAS_ONLY_PARTIAL:-false}" = true ]; then
        _alert_kind=extras
        _cur_gap="$(_extras_gap_fingerprint)"
        _ALERTED_GAP="$_cur_gap"  # announced now, or already announced by the last run
        [ "${_prev_alerted_gap:-}" = "${_cur_gap:--}" ] && _alert_kind=""
    else
        _alert_kind=core
        _ALERTED_CORE=true
        [ "$_prev_core_alerted" = "True" ] && _alert_kind=""
    fi
    if [ -n "$_alert_kind" ]; then
        # Escrow drift withheld the SQL dump from the off-site snapshot (the
        # target is fine — the escrowed passphrase is stale), so name the real
        # cause + fix instead of pointing at the off-site target.
        if [ "$_alert_kind" = extras ]; then
            _send_telegram "⚠️ *Off-site copy incomplete — extra directories*

The core backup reached the off-site snapshot and is restorable, but the
extra directories are incomplete: ${_EXTRA_SKIPPED:-0} not archived this run,
${_EXTRA_UPLOAD_FAILED:-0} not uploaded, ${_EXTRA_PARTIAL:-0} archived without members a
restore would refuse. Missing or partial in this snapshot:
$(printf '%s\n' "${_EXTRA_SKIP_LABELS[@]+"${_EXTRA_SKIP_LABELS[@]}"}" "${_EXTRA_PARTIAL_LABELS[@]+"${_EXTRA_PARTIAL_LABELS[@]/%/ (partial)}"}" | head -5)
Time: $(date -Is)"
        elif [ "$_SQL_ESCROW_DRIFT" = true ]; then
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
