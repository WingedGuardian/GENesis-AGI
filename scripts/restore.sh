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
#   GENESIS_BACKUP_TIER2_BACKEND — off-site backend: none|local|smb (default: smb if
#                                GENESIS_BACKUP_NAS is set, else none). The DB/Qdrant/
#                                transcripts live ONLY off-site (not git).
#   GENESIS_BACKUP_LOCAL_PATH  — local/mounted off-site dir (when backend=local)
#   GENESIS_BACKUP_NAS         — SMB share for the off-site pull (e.g. //nas/share),
#                                when backend=smb
#   GENESIS_BACKUP_NAS_USER    — SMB username for the off-site pull
#   GENESIS_BACKUP_NAS_PASS    — SMB password for the off-site pull
#   GENESIS_BACKUP_NAS_HOST    — SOURCE host name the snapshot was backed up under
#                                (default: this host; set it on a fresh DR box
#                                whose hostname differs — or rely on auto-detect
#                                when only one host exists on the NAS)
#
# Behavior:
#   - Skips destinations that already exist AND are newer than the backup
#     (avoid clobbering live data). Override with --force.
#   - Reads both encrypted (*.gpg) and legacy plaintext forms for backward
#     compatibility with backups predating the encryption hardening.
#   - Writes ~/.genesis/restore_status.json on every run (success or failure).
set -euo pipefail

# Pluggable Tier-2 (off-site) backend interface — selects none/local/smb at runtime
# (backward-compat: a configured GENESIS_BACKUP_NAS with no selector → smb).
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/backup_backends.sh
source "$_SCRIPT_DIR/lib/backup_backends.sh"

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
_EVAL_RESTORED=0
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
{"timestamp":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","success":$_SUCCESS,"dry_run":$DRY_RUN,"sqlite_restored":$_SQLITE_RESTORED,"qdrant_restored":$_QDRANT_RESTORED,"transcripts_restored":$_TRANSCRIPT_RESTORED,"memory_restored":$_MEMORY_RESTORED,"eval_restored":$_EVAL_RESTORED,"cc_memory_restored":$_CCMEM_RESTORED,"overlays_restored":$_OVERLAYS_RESTORED,"secrets_restored":$_SECRETS_RESTORED,"duration_s":$_duration,"failures":$_failures_json}
STATUSEOF
}
# ── Deploy-in-progress marker ────────────────────────────────────────
# While restore.sh holds the server stopped to rebuild the DB (a multi-minute
# `.read` of a ~269MB dump), the autonomy watchdog (genesis-watchdog.timer,
# ~every 300s) would otherwise see the inactive unit and restart genesis-server
# into a HALF-BUILT database — which the 6h backup timer can then snapshot as the
# newest COMPLETE backup. We hold the same marker `env.update_in_progress()`
# already honors (~/.genesis/update_in_progress.pid, a bare live PID) so the
# watchdog DEFERS its restart until the restore's EXIT trap clears it. We refuse
# to clobber a marker a real update.sh/dashboard deploy already owns, and remove
# it only if it is still OUR pid — never another deploy's.
_UPDATE_PID_FILE="${GENESIS_HOME:-$HOME/.genesis}/update_in_progress.pid"
_WROTE_UPDATE_MARKER=false
_acquire_deploy_marker() {
    mkdir -p "$(dirname "$_UPDATE_PID_FILE")"
    if [ -f "$_UPDATE_PID_FILE" ]; then
        local _other
        _other="$(cat "$_UPDATE_PID_FILE" 2>/dev/null || true)"
        if [[ "$_other" =~ ^[0-9]+$ ]] && [ "$_other" -gt 1 ] && kill -0 "$_other" 2>/dev/null; then
            warn "a deploy already holds $_UPDATE_PID_FILE (pid $_other) — not overwriting; a concurrent update+restore is unsafe, verify the result"
            return 0
        fi
    fi
    echo "$$" > "$_UPDATE_PID_FILE"
    _WROTE_UPDATE_MARKER=true
    log "Holding deploy-in-progress marker (pid $$) so the watchdog won't revive genesis-server mid-restore"
}
_release_deploy_marker() {
    $_WROTE_UPDATE_MARKER || return 0
    # Remove only if it is still OUR pid (a later deploy may have taken over).
    if [ -f "$_UPDATE_PID_FILE" ] && [ "$(cat "$_UPDATE_PID_FILE" 2>/dev/null || true)" = "$$" ]; then
        rm -f "$_UPDATE_PID_FILE"
    fi
}
trap '_write_status; _release_deploy_marker; backend_cleanup' EXIT

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

# Large intermediate files (the ~269MB decrypted SQLite .dump, decrypted Qdrant snapshots)
# must NOT land in the inherited TMPDIR (cc-tmp = the watchgod "oxygen" folder for a CC run;
# /tmp tmpfs/RAM otherwise). Route them to a dedicated on-disk dir per the tmp_filesystem_limit
# procedure. NOT an `export TMPDIR` — only the big files move.
GENESIS_BIG_TMP="${GENESIS_BACKUP_TMPDIR:-$HOME/tmp}"
mkdir -p "$GENESIS_BIG_TMP"
log "big-temp dir: $GENESIS_BIG_TMP"

# Quiesce the live writer before swapping the SQLite DB — a live WAL connection
# would corrupt the restore. Guarded for fresh-box DR (no systemctl / no unit /
# no user session → no-op). Intentionally does NOT restart: the operator
# verifies the restored DB first, then starts the server.
_SERVER_WAS_STOPPED=false
_quiesce_genesis_server() {
    command -v systemctl >/dev/null 2>&1 || return 0
    # Acquire the deploy marker UNCONDITIONALLY (systemctl exists → a watchdog
    # could run). The watchdog revives an INACTIVE unit, so the marker matters
    # MOST when the server is already stopped at restore start — a cautious
    # operator may `systemctl --user stop genesis-server` before restoring, or it
    # may have crashed. Gating the marker on is-active (as an earlier draft did)
    # would leave that highest-risk case — the multi-minute .read — unprotected.
    # Only the stop ACTION below is gated on liveness.
    _acquire_deploy_marker
    if systemctl --user is-active --quiet genesis-server 2>/dev/null; then
        log "Stopping genesis-server before SQLite restore (will NOT auto-restart)..."
        # Only record "stopped" if the stop actually succeeded — otherwise the
        # end-of-run note would tell the operator to restart a server that never
        # stopped (and is still holding the DB).
        if systemctl --user stop genesis-server 2>/dev/null; then
            _SERVER_WAS_STOPPED=true
        else
            warn "could not stop genesis-server — proceeding (a live writer may still hold the DB; verify before trusting the restore)"
        fi
    fi
}

_BACKUP_PASSPHRASE="${GENESIS_BACKUP_PASSPHRASE:-}"

# Circular-trap fallback: if the passphrase is not in the environment (e.g.
# secrets.env was lost — the exact disaster this backup exists for), read it
# from the host-side escrow the credential bridge writes. Without this, an
# encrypted backup of a lost secrets.env would be undecryptable.
if [ -z "$_BACKUP_PASSPHRASE" ]; then
    for _escrow in \
        "${GENESIS_PASSPHRASE_ESCROW:-}" \
        "$HOME/.genesis/shared/guardian/backup_passphrase.env" \
        "$HOME/.local/state/genesis-guardian/shared/guardian/backup_passphrase.env" \
        "$HOME/.local/state/genesis-guardian/creds-archive/backup_passphrase.env"; do
        [ -n "$_escrow" ] && [ -f "$_escrow" ] || continue
        # Bridge writes exactly `GENESIS_BACKUP_PASSPHRASE=<value>` (no quotes);
        # tolerate an optional `export ` prefix. Do NOT strip quotes — the
        # passphrase may legitimately contain them.
        _escrowed="$(sed -n 's/^\(export \)\{0,1\}GENESIS_BACKUP_PASSPHRASE=//p' "$_escrow" | head -n1)"
        if [ -n "$_escrowed" ]; then
            _BACKUP_PASSPHRASE="$_escrowed"
            log "Using escrowed backup passphrase from $_escrow (env passphrase absent)"
            break
        fi
    done
fi

# G.4 host-side credential mirror fallback. If the Tier-1 backup clone lacks the
# encrypted creds/secrets payloads (e.g. a freshly rebuilt container whose backup
# clone has not been re-cloned yet), fall back to the host-side mirror on the
# shared mount — or, for a host-side restore, the guardian's host-only archive.
# Ordered candidate roots (shared mount preferred, then the host-only archive).
# Sections 7/8 resolve the secrets source and the creds source INDEPENDENTLY,
# each picking the first candidate that carries ITS OWN payload — so a partial
# mirror (e.g. creds present but secrets lost) never masks a complete archive.
_cred_fallback_sources() {
    printf '%s\n' \
        "${GENESIS_CREDS_MIRROR:-}" \
        "$HOME/.genesis/shared/guardian/creds-mirror" \
        "$HOME/.local/state/genesis-guardian/shared/guardian/creds-mirror" \
        "$HOME/.local/state/genesis-guardian/creds-archive"
}

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

# ── Off-site (backend) pull ──────────────────────────────────────────
# The large binaries (SQLite dump, Qdrant snapshots, transcripts) live ONLY on the
# off-site backend — gitignored from Tier-1 — so on a fresh DR box they must be
# pulled from the latest dated COMPLETE snapshot before the restore sections below
# can find them. Destination is the pluggable backend (none/local/smb).
_pull_from_offsite() {
    backend_init
    # Clean the backend's transient creds when this function returns (tighter than
    # the script-wide EXIT trap, which also calls it — backend_cleanup is idempotent).
    trap 'backend_cleanup' RETURN
    local be
    be="$(backend_name)"
    [ "$be" = "none" ] && return 0
    if $DRY_RUN; then log "off-site: (dry-run) would pull the latest snapshot via the $be backend"; return 0; fi
    backend_available || { log "off-site: backend '$be' is not available — skipping off-site pull"; return 0; }
    # Don't clobber a payload already staged locally (same-box re-run) unless
    # forced — the off-site pull is for fresh-box DR.
    if [ -f "$BACKUP_DIR/data/genesis.sql.gpg" ] && ! $FORCE; then
        log "off-site: local payload already present — skipping off-site pull (use --force to override)"
        return 0
    fi

    local host_dir off_host latest snap fname dst hosts n

    # Latest snapshot under host dir $1 that is COMPLETE — a marker backup.sh writes
    # only after every file uploaded — so a half-uploaded snapshot from a crashed
    # backup is never selected. Echoes the stamp; returns 1 if none. Newest first.
    _latest_complete() {
        local hd="$1" st
        while read -r st; do
            [ -n "$st" ] || continue
            backend_exists "$hd/$st/COMPLETE" && { echo "$st"; return 0; }
        done < <(backend_list_dirs "$hd" | grep -oE '[0-9]{8}T[0-9]{6}Z' | sort -ru)
        return 1
    }

    # The snapshot was written under the SOURCE host's name. On a fresh DR box the
    # hostname differs, so honour an explicit override (GENESIS_BACKUP_NAS_HOST),
    # and otherwise fall back to the sole host dir when there's exactly one.
    off_host="${GENESIS_BACKUP_NAS_HOST:-$(hostname)}"
    host_dir="Genesis/$off_host"
    latest="$(_latest_complete "$host_dir" || true)"
    if [ -z "$latest" ]; then
        hosts=$(backend_list_dirs "Genesis" || true)
        n=$(printf '%s' "$hosts" | grep -c . || true)
        if [ "$n" = 1 ]; then
            host_dir="Genesis/$hosts"
            log "off-site: no snapshots under host '$off_host' — using the only host: $hosts"
            latest="$(_latest_complete "$host_dir" || true)"
        fi
    fi
    if [ -z "$latest" ]; then
        log "off-site: no COMPLETE dated snapshot found (set GENESIS_BACKUP_NAS_HOST to the source host name) — skipping off-site pull"
        return 0
    fi
    log "off-site: pulling latest snapshot $latest (backend: $be)"
    snap="$host_dir/$latest"

    # SQLite dump.
    mkdir -p "$BACKUP_DIR/data"
    if backend_get "$snap/data/genesis.sql.gpg" "$BACKUP_DIR/data/genesis.sql.gpg"; then
        log "  off-site: pulled data/genesis.sql.gpg"
    else
        warn "off-site: failed to pull genesis.sql.gpg from snapshot $latest — the database will not be restored from off-site"
    fi
    # Qdrant snapshots + transcripts: list the subdir, then get each *.gpg. The
    # staging dir is created only when there's actually something to pull, so an
    # empty set doesn't make the restore sections below run (and e.g. warn that
    # Qdrant isn't reachable yet → spurious failure).
    for sub in qdrant transcripts; do
        dst="$BACKUP_DIR/data/qdrant"
        [ "$sub" = transcripts ] && dst="$BACKUP_DIR/transcripts"
        # `|| true`: an empty subdir makes `grep` exit non-zero → pipefail would
        # otherwise abort the whole restore.
        backend_list "$snap/$sub" | grep -oE '[A-Za-z0-9._-]+\.gpg' | sort -u | while read -r fname; do
            mkdir -p "$dst"
            if backend_get "$snap/$sub/$fname" "$dst/$fname"; then
                log "  off-site: pulled $sub/$fname"
            fi
        done || true
    done

    # memory / config overlays / secrets — previously only in the Tier-1 git clone. Pull
    # them from the snapshot too so a no-git fresh box can rehydrate them (the §4/§6/§7
    # restore sections read from these BACKUP_DIR subdirs). memory is flat; config overlays
    # are plaintext .local.yaml; secrets is the encrypted blob. Staging dirs are created
    # only when there's something to pull.
    #
    # Process substitution (not a `… | while`) is deliberate: a failed pull of these
    # payloads is the silent DR footgun this PR exists to prevent, so a failed get must
    # `warn` (→ _FAILURES → non-zero restore). A pipe-into-while runs the body in a
    # SUBSHELL where _FAILURES appends are lost; `done < <(…)` runs it in THIS shell.
    while read -r fname; do
        mkdir -p "$BACKUP_DIR/memory"
        if backend_get "$snap/memory/$fname" "$BACKUP_DIR/memory/$fname"; then
            log "  off-site: pulled memory/$fname"
        else
            warn "off-site: failed to pull memory/$fname from snapshot $latest"
        fi
    done < <(backend_list "$snap/memory" | grep -oE '[A-Za-z0-9._-]+\.gpg' | sort -u)
    while read -r fname; do
        mkdir -p "$BACKUP_DIR/config_overrides"
        if backend_get "$snap/config_overrides/$fname" "$BACKUP_DIR/config_overrides/$fname"; then
            log "  off-site: pulled config_overrides/$fname"
        else
            warn "off-site: failed to pull config_overrides/$fname from snapshot $latest"
        fi
    done < <(backend_list "$snap/config_overrides" | grep -oE '[A-Za-z0-9._-]+\.local\.yaml' | sort -u)
    if backend_exists "$snap/secrets/secrets.env.gpg"; then
        mkdir -p "$BACKUP_DIR/secrets"
        if backend_get "$snap/secrets/secrets.env.gpg" "$BACKUP_DIR/secrets/secrets.env.gpg"; then
            log "  off-site: pulled secrets/secrets.env.gpg"
        else
            warn "off-site: failed to pull secrets.env.gpg from snapshot $latest — secrets will not be restored"
        fi
    fi
    # eval golden sets — a no-git fresh box needs them from the snapshot too
    # (restore §4b reads $BACKUP_DIR/eval). backend_list is single-level, so
    # iterate eval/ and eval/golden/ separately; the .gpg filter drops the
    # `golden` subdir entry so it is not mis-fetched as a flat file.
    for _sub in eval eval/golden; do
        while read -r fname; do
            mkdir -p "$BACKUP_DIR/$_sub"
            if backend_get "$snap/$_sub/$fname" "$BACKUP_DIR/$_sub/$fname"; then
                log "  off-site: pulled $_sub/$fname"
            else
                warn "off-site: failed to pull $_sub/$fname from snapshot $latest"
            fi
        done < <(backend_list "$snap/$_sub" 2>/dev/null | grep -oE '[A-Za-z0-9._-]+\.gpg' | sort -u)
    done
    # creds — Tier-1 git normally carries these; a no-git box needs them from the
    # snapshot too (restore §8 reads $BACKUP_DIR/creds). backend_list is
    # single-level, so iterate creds/ and creds/ssh/ separately; the .gpg filter
    # drops the `ssh` subdir entry so it is not mis-fetched as a flat file.
    for _sub in creds creds/ssh; do
        while read -r fname; do
            mkdir -p "$BACKUP_DIR/$_sub"
            if backend_get "$snap/$_sub/$fname" "$BACKUP_DIR/$_sub/$fname"; then
                log "  off-site: pulled $_sub/$fname"
            else
                warn "off-site: failed to pull $_sub/$fname from snapshot $latest"
            fi
        done < <(backend_list "$snap/$_sub" 2>/dev/null | grep -oE '[A-Za-z0-9._-]+\.gpg' | sort -u)
    done
}
_pull_from_offsite

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
            _SQL_TMP=$(mktemp -p "$GENESIS_BIG_TMP")  # ~269MB dump — keep off cc-tmp/RAM
            if $__PAYLOAD_NEEDS_DECRYPT; then
                decrypt_file "$src" "$_SQL_TMP" || { warn "SQLite decrypt failed"; rm -f "$_SQL_TMP"; }
            else
                cp "$src" "$_SQL_TMP"
            fi
            if [ -s "$_SQL_TMP" ]; then
                # Fresh DB from the SQL dump. Stop the live writer FIRST — both
                # the pre-restore safety copy AND the new DB must be taken with
                # no open WAL connection, or they are torn/stale.
                _quiesce_genesis_server
                # Back up the existing DB before we overwrite it. Taken AFTER
                # quiescing and via `sqlite3 .backup` (WAL-aware) so the undo
                # artifact is a consistent snapshot — the old behavior did a
                # plain `cp` of only the main db file BEFORE quiescing, missing
                # the -wal, so the sole rollback copy was torn exactly when an
                # operator needs it to undo a bad restore. Fall back to copying
                # the db + its sidecars together if sqlite3 is unavailable.
                if [ -f "$DB_FILE" ]; then
                    _PRE_RESTORE="${DB_FILE}.pre-restore.$(date +%s)"
                    if command -v sqlite3 >/dev/null && sqlite3 "$DB_FILE" ".backup '$_PRE_RESTORE'" 2>/dev/null; then
                        log "SQLite: pre-restore safety copy → $_PRE_RESTORE (sqlite3 .backup, WAL-correct)"
                    else
                        cp "$DB_FILE" "$_PRE_RESTORE"
                        [ -f "$DB_FILE-wal" ] && cp "$DB_FILE-wal" "${_PRE_RESTORE}-wal"
                        [ -f "$DB_FILE-shm" ] && cp "$DB_FILE-shm" "${_PRE_RESTORE}-shm"
                        log "SQLite: pre-restore safety copy → $_PRE_RESTORE (cp + sidecars; sqlite3 unavailable)"
                    fi
                fi
                # Clear stale WAL/SHM sidecars — a leftover -wal would replay
                # onto the new DB and corrupt it.
                rm -f "$DB_FILE" "$DB_FILE-wal" "$DB_FILE-shm"
                if command -v sqlite3 >/dev/null; then
                    if sqlite3 "$DB_FILE" ".read $_SQL_TMP"; then
                        _SQLITE_RESTORED=true
                        log "SQLite: restored → $DB_FILE"
                        # Verify the restored DB is structurally sound — loud on failure.
                        # 2>&1 so a sqlite3 error (can't open, etc.) surfaces in the warn.
                        _ic=$(sqlite3 "$DB_FILE" "PRAGMA integrity_check;" 2>&1 | head -1)
                        if [ "$_ic" = "ok" ]; then
                            log "SQLite: integrity_check ok"
                        else
                            warn "SQLite: integrity_check FAILED (${_ic:-no output}) — restored DB may be corrupt; inspect ${DB_FILE}.pre-restore.*"
                        fi
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
                _QDRANT_TMP=$(mktemp -p "$GENESIS_BIG_TMP" --suffix=.snapshot)  # large — keep off cc-tmp/RAM
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

# ── 4b. Eval golden sets ─────────────────────────────────────────────
log "--- Eval golden sets ---"
if [ -d "$BACKUP_DIR/eval" ]; then
    _EVAL_TARGET="$HOME/.genesis/eval"
    mkdir -p "$_EVAL_TARGET"
    while IFS= read -r -d '' src; do
        rel="${src#"$BACKUP_DIR"/eval/}"
        dst_rel="${rel%.gpg}"
        dst="$_EVAL_TARGET/$dst_rel"
        if [ -f "$dst" ] && [ "$dst" -nt "$src" ] && ! $FORCE; then
            continue
        fi
        if $DRY_RUN; then
            log "Eval: would restore $rel → $dst"
            _EVAL_RESTORED=$(( _EVAL_RESTORED + 1 ))
            continue
        fi
        mkdir -p "$(dirname "$dst")"
        if [[ "$src" == *.gpg ]]; then
            decrypt_file "$src" "$dst" || { warn "eval decrypt failed: $rel"; continue; }
        else
            cp "$src" "$dst"
        fi
        _EVAL_RESTORED=$(( _EVAL_RESTORED + 1 ))
    done < <(find "$BACKUP_DIR/eval" -type f -print0 2>/dev/null)
    log "Eval golden sets: $_EVAL_RESTORED restored"
else
    log "Eval golden sets: no backup directory"
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
if [ ! -f "$SECRETS_SRC" ]; then
    while IFS= read -r _d; do
        [ -n "$_d" ] && [ -f "$_d/secrets/secrets.env.gpg" ] || continue
        SECRETS_SRC="$_d/secrets/secrets.env.gpg"
        log "Secrets: Tier-1 clone payload absent — using host-side mirror $SECRETS_SRC"
        break
    done < <(_cred_fallback_sources)
fi
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

# ── 8. Critical credential & wiring files → staging (non-destructive) ─
# Decrypted to a staging dir, never auto-placed: clobbering a live ~/.ssh key or
# credential file mid-restore is dangerous. On a fresh rebuild, move them into
# place from the staging dir (paths logged below). These live in the Tier-1 git
# clone, so no off-site pull is needed.
log "--- Credential & wiring files ---"
CREDS_SRC_DIR="$BACKUP_DIR/creds"
# Key the fallback on actual .gpg PAYLOAD presence, not directory existence:
# backup.sh skips creds when the passphrase was unset (§8), which can leave an
# empty/placeholder creds/ dir; and a partial mirror may have creds/ without the
# .gpg files. Pick the first candidate that actually carries creds payload, so a
# hollow mirror never masks a complete archive. set -e-safe (checks in `if`).
_creds_has_payload() { find "$1" -name '*.gpg' -print -quit 2>/dev/null | grep -q .; }
if ! { [ -d "$CREDS_SRC_DIR" ] && _creds_has_payload "$CREDS_SRC_DIR"; }; then
    while IFS= read -r _d; do
        [ -n "$_d" ] && [ -d "$_d/creds" ] && _creds_has_payload "$_d/creds" || continue
        CREDS_SRC_DIR="$_d/creds"
        log "Creds: Tier-1 clone payload absent — using host-side mirror $CREDS_SRC_DIR"
        break
    done < <(_cred_fallback_sources)
fi
if [ -d "$CREDS_SRC_DIR" ]; then
    CREDS_STAGE="${GENESIS_CREDS_STAGE:-$HOME/.genesis/restore-creds}"
    _CREDS_STAGED=0
    # Private-by-creation: make the stage dir 0700 and set umask 077 BEFORE any
    # plaintext is written, so decrypted SSH keys / credentials are never briefly
    # world-readable on a multi-user host (no window between write and chmod).
    if ! $DRY_RUN; then
        mkdir -p "$CREDS_STAGE" && chmod 0700 "$CREDS_STAGE"
    fi
    _prev_umask="$(umask)"; umask 077
    while IFS= read -r -d '' _gpg; do
        _rel="${_gpg#"$CREDS_SRC_DIR"/}"       # e.g. ssh/id_ed25519.gpg
        _out="$CREDS_STAGE/${_rel%.gpg}"        # strip trailing .gpg
        if $DRY_RUN; then
            log "Creds: would decrypt → $_out"
            continue
        fi
        mkdir -p "$(dirname "$_out")"
        if decrypt_file "$_gpg" "$_out"; then
            chmod 0600 "$_out"
            _CREDS_STAGED=$(( _CREDS_STAGED + 1 ))
        else
            warn "Creds: decrypt failed for $_rel"
        fi
    done < <(find "$CREDS_SRC_DIR" -type f -name '*.gpg' -print0)
    umask "$_prev_umask"
    if ! $DRY_RUN; then
        log "Creds: $_CREDS_STAGED file(s) decrypted → $CREDS_STAGE (staged, NOT auto-placed)"
        log "      Move into place manually (ssh/ → ~/.ssh/, gh_hosts.yml → ~/.config/gh/hosts.yml, etc.)."
    fi
else
    log "Creds: no backup payload at $CREDS_SRC_DIR"
fi

# ── Done ─────────────────────────────────────────────────────────────
if $_SERVER_WAS_STOPPED; then
    log "NOTE: genesis-server was stopped for the restore and left stopped."
    log "      Verify the restored DB, then: systemctl --user start genesis-server"
fi
if [ ${#_FAILURES[@]} -eq 0 ]; then
    _SUCCESS=true
    log "Restore complete"
else
    log "Restore complete with ${#_FAILURES[@]} warning(s):"
    for f in "${_FAILURES[@]}"; do log "  - $f"; done
    # Exit non-zero so CI / cron can flag partial restores.
    exit 1
fi
