#!/usr/bin/env bash
# Genesis restore — counterpart to scripts/backup.sh.
# Reads your genesis-backups repo and rehydrates Genesis state:
# SQLite, Qdrant collections, CC transcripts, auto-memory, CC memory,
# local config overlays, and secrets.
#
# Usage:
#   scripts/restore.sh [--from <backup-repo-url>] [--dry-run] [--force] [--database-only]
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
#     (avoid clobbering live data). Override with --force. Database-only recovery
#     may replace a currently quarantined DB regardless of its meaningless mtime.
#   - Reads both encrypted (*.gpg) and legacy plaintext forms for backward
#     compatibility with backups predating the encryption hardening.
#   - Writes ~/.genesis/restore_status.json on every run (success or failure).
set -euo pipefail

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
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/backup_backends.sh
source "$_SCRIPT_DIR/lib/backup_backends.sh"

# ── Args ─────────────────────────────────────────────────────────────
BACKUP_REPO_OVERRIDE=""
DRY_RUN=false
FORCE=false
DATABASE_ONLY=false
while [ $# -gt 0 ]; do
    case "$1" in
        --from) BACKUP_REPO_OVERRIDE="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        --force) FORCE=true; shift ;;
        --database-only) DATABASE_ONLY=true; shift ;;
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
            log "ERROR: a deploy already holds $_UPDATE_PID_FILE (pid $_other) — refusing concurrent update+restore"
            return 1
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
# N2: the credential-bearing plaintext SQL dump and decrypted Qdrant snapshot
# temps are `rm`'d inline, but a mid-section death would leave them in ~/tmp —
# trap-protect them. (Empty-string default → no-op before they're assigned.)
_SQL_TMP=""
_QDRANT_TMP=""
_DB_STAGE=""
_cleanup_plaintext() {
    rm -f "${_SQL_TMP:-}" "${_QDRANT_TMP:-}" 2>/dev/null || true
    if [ -n "${_DB_STAGE:-}" ]; then
        rm -f "$_DB_STAGE" "$_DB_STAGE-journal" "$_DB_STAGE-wal" "$_DB_STAGE-shm" 2>/dev/null || true
    fi
}
trap '_write_status; _release_deploy_marker; backend_cleanup; _cleanup_plaintext' EXIT

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

# Serialize with scripts/update.sh as well as backup.sh. Both restore and update
# stop services and own the same deploy marker. Acquire locks in the universal
# order update → backup/restore: update.sh holds update while invoking backup,
# so the reverse order here could deadlock an update against a restore.
_UPDATE_LOCK_FILE="${GENESIS_HOME:-$HOME/.genesis}/locks/update.lock"
mkdir -p "$(dirname "$_UPDATE_LOCK_FILE")"
exec {_RESTORE_UPDATE_LOCK_FD}>"$_UPDATE_LOCK_FILE"
if ! flock -n "$_RESTORE_UPDATE_LOCK_FD"; then
    die "Genesis update lock is held — refusing concurrent update+restore"
fi

# ── Mutual exclusion (SF5): backup↔restore share one whole-run lock ──
# Counterpart of backup.sh's non-blocking skip. A restore is operator-driven,
# so it WAITS (bounded) rather than skipping. Acquired only after update.lock
# to preserve the global lock order above.
# shellcheck source=scripts/lib/dr_lock.sh
source "$_SCRIPT_DIR/lib/dr_lock.sh"
_LOCK_WAIT="${GENESIS_RESTORE_LOCK_WAIT:-300}"
dr_lock_open
if ! flock -w "$_LOCK_WAIT" "$DR_LOCK_FD"; then
    _holder="$(cat "$DR_LOCK_FILE" 2>/dev/null || true)"
    die "backup-restore lock still held by ${_holder:-unknown} after ${_LOCK_WAIT}s — a backup is likely running; wait for it to finish and re-run (or set GENESIS_RESTORE_LOCK_WAIT higher)"
fi
dr_lock_stamp restore

# Private-by-default for every plaintext this restore writes (SF7): gpg -d and
# cp otherwise honor the inherited umask (typically 0022 → world-readable), so a
# decrypted secrets.env / transcript / memory file (all PII-bearing) would be
# briefly readable by other users on a multi-user host between write and any
# chmod. Set once here — before the first write — so no section carries that
# window. (§8 creds still sets its own umask 077 for defence in depth.) The
# restored DB and config overlays become 0600 too, which is strictly correct;
# genesis-server reads them as the same user.
umask 077

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
    _acquire_deploy_marker || die "another live deploy owns the deploy marker — live database left untouched"
    if systemctl --user is-active --quiet genesis-server 2>/dev/null; then
        log "Stopping genesis-server before SQLite restore (will NOT auto-restart)..."
        # Only record "stopped" if the stop actually succeeded — otherwise the
        # end-of-run note would tell the operator to restart a server that never
        # stopped (and is still holding the DB).
        if systemctl --user stop genesis-server 2>/dev/null \
            && ! systemctl --user is-active --quiet genesis-server 2>/dev/null; then
            _SERVER_WAS_STOPPED=true
        else
            die "could not confirm genesis-server stopped — live database left untouched"
        fi
    fi
    # Older installs may still have the deprecated relay running with its own
    # database handle. Stop it too; otherwise the open-handle fence below must
    # abort an otherwise valid recovery.
    if systemctl --user is-active --quiet genesis-bridge 2>/dev/null; then
        log "Stopping legacy genesis-bridge before SQLite restore (will NOT auto-restart)..."
        if ! systemctl --user stop genesis-bridge 2>/dev/null \
            || systemctl --user is-active --quiet genesis-bridge 2>/dev/null; then
            die "could not confirm genesis-bridge stopped — live database left untouched"
        fi
    fi
}

_BACKUP_PASSPHRASE="${GENESIS_BACKUP_PASSPHRASE:-}"

# Circular-trap fallback: if the passphrase is not in the environment (e.g.
# secrets.env was lost — the exact disaster this backup exists for), read it
# from the host-side escrow the credential bridge writes. Without this, an
# encrypted backup of a lost secrets.env would be undecryptable. Lookup logic
# lives in lib/passphrase_escrow.sh (shared with backup.sh's SF4 round-trip).
# shellcheck source=scripts/lib/passphrase_escrow.sh
source "$_SCRIPT_DIR/lib/passphrase_escrow.sh"
if [ -z "$_BACKUP_PASSPHRASE" ]; then
    passphrase_escrow_lookup
    if [ -n "$ESCROW_PASSPHRASE" ]; then
        _BACKUP_PASSPHRASE="$ESCROW_PASSPHRASE"
        log "Using escrowed backup passphrase from $ESCROW_SOURCE (env passphrase absent)"
    fi
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
    local prompt="$1" reply
    $FORCE && return 0
    $DRY_RUN && return 0
    # `read` returning non-zero means EOF (no TTY to answer), NOT a decline —
    # without this, an unattended run (e.g. `python -m genesis restore` with no
    # --force) has EVERY confirm silently return false, skips every section, and
    # exits 0 "success" having restored nothing. Refuse loudly instead. (N4)
    if ! read -r -p "$prompt [y/N] " reply; then
        die "no TTY to confirm '$prompt' — re-run with --force for an unattended restore"
    fi
    [[ "$reply" =~ ^[yY]([eE][sS])?$ ]]
}

# ── Obtain backup repo ───────────────────────────────────────────────
_EXPLICIT_LOCAL_SOURCE=false
if [ -n "$BACKUP_REPO_OVERRIDE" ]; then
    if [ -d "$BACKUP_REPO_OVERRIDE/.git" ] || [ -d "$BACKUP_REPO_OVERRIDE" ]; then
        # Treat as local path
        BACKUP_DIR="$BACKUP_REPO_OVERRIDE"
        _EXPLICIT_LOCAL_SOURCE=true
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
    # Qdrant snapshots + transcripts: list the subdir, then get each *.gpg.
    # Process substitution (not `list | grep | while`): a failed backend_get of
    # these — the two LARGEST DR payloads (vectors + the "permanent archive"
    # transcripts) — must `warn` (→ _FAILURES → non-zero restore), matching the
    # memory/config/secrets pulls below. A pipe-into-while runs the body in a
    # SUBSHELL where the _FAILURES append is lost, and its `|| true` masked the
    # failure entirely — the silent-DR-footgun this converts away from. The
    # `|| true` on the process-substitution input still absorbs an empty-subdir
    # grep-miss (staging dir created only when something is actually pulled).
    for sub in qdrant transcripts; do
        dst="$BACKUP_DIR/data/qdrant"
        [ "$sub" = transcripts ] && dst="$BACKUP_DIR/transcripts"
        while read -r fname; do
            mkdir -p "$dst"
            if backend_get "$snap/$sub/$fname" "$dst/$fname"; then
                log "  off-site: pulled $sub/$fname"
            else
                warn "off-site: failed to pull $sub/$fname from snapshot $latest"
            fi
        done < <(backend_list "$snap/$sub" | grep -oE '[A-Za-z0-9._-]+\.gpg' | sort -u || true)
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
if $DATABASE_ONLY && $_EXPLICIT_LOCAL_SOURCE \
    && { [ -f "$BACKUP_DIR/data/genesis.sql.gpg" ] || [ -f "$BACKUP_DIR/data/genesis.sql" ]; }; then
    log "database-only: using explicit local SQL payload without off-site replacement"
else
    _pull_from_offsite
fi

# N5: a restore that finds NO payloads at all (empty/wrong BACKUP_DIR, no
# off-site) used to log "no payload" per section and exit 0 "success" having
# restored nothing — dangerous for an unattended DR run pointed at the wrong
# place. Fail loudly up front instead. (A backup that IS present but whose
# destinations are all newer legitimately no-ops later — that's found-but-
# skipped, not this empty case, so it still succeeds.)
# Any-file (not just *.gpg): §4/§4b restore memory/eval with `find -type f`, so a
# legacy plaintext memory file (arbitrary extension) IS restorable and must count
# here — matching what the sections actually accept, or the guard false-fails a
# valid legacy backup.
_dir_has_file() { [ -d "$1" ] && find "$1" -type f -print -quit 2>/dev/null | grep -q .; }
_backup_has_payload() {
    local d="$BACKUP_DIR" _m
    { [ -f "$d/data/genesis.sql.gpg" ] || [ -f "$d/data/genesis.sql" ]; } && return 0
    _dir_has_file "$d/data/qdrant" && return 0
    _dir_has_file "$d/transcripts" && return 0
    _dir_has_file "$d/memory" && return 0
    _dir_has_file "$d/eval" && return 0
    _dir_has_file "$d/creds" && return 0
    # §6d restores this store, so it is a restorable payload and must be counted
    # here or a backup whose ONLY surviving payload is the audit trail dies at the
    # guard below and never reaches the section that would restore it — after this
    # change advertised it as a Tier-1 payload (Codex P2, PR #1609). A partial
    # Tier-1 recovery, or a run that skipped the encrypted sections, is exactly
    # when that shape occurs.
    # Matched to what §6d actually restores (`-name '*.jsonl'`), NOT the generic
    # `_dir_has_file`, which accepts any file: a backup killed between staging a
    # mirror copy and sweeping leaves a `.<name>.partial.<pid>` scrap, and a mirror
    # holding zero restorable records would otherwise satisfy this guard and let the
    # run report success having restored nothing.
    find "$d/audit/merge_overrides" -maxdepth 1 -type f -name '*.jsonl' -print -quit \
        2>/dev/null | grep -q . && return 0
    [ -f "$d/secrets/secrets.env.gpg" ] && return 0
    find "$d/config_overrides" -type f -name '*.local.yaml' -print -quit 2>/dev/null | grep -q . && return 0
    # §7/§8 also restore secrets/creds from the host-side credential MIRROR when
    # BACKUP_DIR lacks them (a no-git DR box whose only surviving copy is the
    # guardian mirror) — so a mirror payload counts too, or the guard would
    # wrongly refuse that recovery path.
    while IFS= read -r _m; do
        [ -n "$_m" ] || continue
        [ -f "$_m/secrets/secrets.env.gpg" ] && return 0
        _dir_has_file "$_m/creds" && return 0
    done < <(_cred_fallback_sources)
    return 1
}
if ! $DRY_RUN && ! _backup_has_payload; then
    die "no restorable payloads found under $BACKUP_DIR (empty or wrong backup source, and no off-site snapshot pulled) — nothing to restore"
fi
if $DATABASE_ONLY \
    && [ ! -f "$BACKUP_DIR/data/genesis.sql.gpg" ] \
    && [ ! -f "$BACKUP_DIR/data/genesis.sql" ]; then
    die "database-only restore requires a SQLite payload"
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
    _RECOVERING_QUARANTINED_DB=false
    if $DATABASE_ONLY && [ -f "$DB_FILE" ]; then
        _QUARANTINE_CHECK_OUTPUT=""
        _QUARANTINE_CHECK_RC=0
        _QUARANTINE_CHECK_OUTPUT=$(PYTHONPATH="$_SCRIPT_DIR/../src" python3 - "$DB_FILE" <<'PY'
import sys

from genesis.db.integrity import database_is_quarantined

raise SystemExit(0 if database_is_quarantined(sys.argv[1]) else 1)
PY
        ) || _QUARANTINE_CHECK_RC=$?
        case "$_QUARANTINE_CHECK_RC" in
            0)
                _RECOVERING_QUARANTINED_DB=true
                log "SQLite: live database is quarantined; verified recovery may replace it regardless of mtime"
                ;;
            1) ;;
            *)
                die "could not determine live database quarantine state (${_QUARANTINE_CHECK_OUTPUT:-checker unavailable})"
                ;;
        esac
    fi
    if [ -f "$DB_FILE" ] && [ "$DB_FILE" -nt "$src" ] \
        && ! $FORCE && ! $_RECOVERING_QUARANTINED_DB; then
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
                command -v sqlite3 >/dev/null \
                    || die "SQLite: sqlite3 binary not installed — live database left untouched"

                # Import and validate away from the live path. No failure before
                # the final rename is allowed to alter the current DB trio.
                _DB_STAGE="${DB_FILE}.restore-stage.$$"
                rm -f "$_DB_STAGE"
                sqlite3 "$_DB_STAGE" ".read $_SQL_TMP" \
                    || die "SQLite .read failed in staging — live database left untouched"
                _ic=$(sqlite3 "$_DB_STAGE" "PRAGMA integrity_check;" 2>&1) \
                    || die "SQLite staged integrity_check could not complete — live database left untouched"
                [ "$_ic" = "ok" ] \
                    || die "SQLite staged integrity_check FAILED (${_ic:-no output}) — live database left untouched"
                _fk=$(sqlite3 "$_DB_STAGE" "PRAGMA foreign_key_check;" 2>&1) \
                    || die "SQLite staged foreign_key_check could not complete — live database left untouched"
                [ -z "$_fk" ] \
                    || die "SQLite staged foreign_key_check FAILED — live database left untouched"
                _schema_count=$(sqlite3 "$_DB_STAGE" \
                    "SELECT count(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%';")
                [ "${_schema_count:-0}" -gt 0 ] \
                    || die "SQLite staged database has no application schema — live database left untouched"
                log "SQLite: staged candidate passed integrity, foreign-key, and schema checks"

                _quiesce_genesis_server
                # Durable crash fence. If power is lost anywhere in the swap,
                # startup fails closed instead of creating an empty DB at a
                # temporarily missing path. A verified replacement inode clears
                # this marker below.
                PYTHONPATH="$_SCRIPT_DIR/../src" python3 -m genesis.db.integrity mark \
                    "$DB_FILE" --source restore-in-progress \
                    --detail "validated candidate staged; database swap in progress" \
                    >/dev/null \
                    || die "could not establish durable database quarantine — live database left untouched"

                # Session-scoped MCP processes can outlive genesis-server. The
                # marker is established before this scan, so all shared open
                # paths refuse from here onward; any already-open handle would
                # keep writing the old inode/WAL after swap and is fatal.
                #
                # Output and exit status are captured SEPARATELY, and BOTH are
                # refusals. The previous form — `find ... | grep -q .` under
                # `set -o pipefail` — reported find's status, so any unreadable
                # /proc entry (routine: other-uid processes are not inspectable
                # from here) discarded grep's success and the guard could not
                # fire at all. Measured with a guaranteed match, it still went
                # silent: a no-op guard on any real box, in the one place built
                # to prevent the corruption that took production down.
                #
                # An inspection that could not complete establishes NOTHING, so
                # it refuses rather than warning. Descriptors are inherited and
                # passable (SCM_RIGHTS), so no property of the current file
                # narrows the holder set for us — visibility must be resolved
                # operationally, by an inspector with sufficient authority or a
                # verified offline boundary, and never assumed.
                # Conclusive visibility needs AUTHORITY, not just a scan: from
                # an unprivileged uid, /proc/<pid>/fd is unreadable for other-uid
                # processes (measured here: 21 of 83 fd dirs), so a plain scan
                # cannot tell "no holder" from "could not look". The narrow
                # privileged read-only `find` below closes that gap — ONLY the
                # scan is privileged; the restore itself never runs as root.
                # Measured: as root, 0 of 86 fd dirs are unreadable and the scan
                # exits clean. Without a way to inspect conclusively we refuse,
                # because an unseen process is an UNKNOWN holder, not an absent
                # one. Descriptors are inherited and passable (SCM_RIGHTS), so no
                # property of the file narrows the holder set for us.
                # Accept ONLY a complete scan: no holders AND no inspection
                # errors. A pid vanishing between the glob and the traversal
                # makes find exit nonzero, so a single attempt refuses roughly
                # 1 in 10 legitimate restores on a healthy box (measured). We
                # retry a bounded number of times rather than classifying error
                # text: the wording differs between find implementations (this
                # box's is bfs) and inferring permission-to-proceed from a
                # message is exactly the fragility to avoid.
                #
                # HOLDERS ARE CHECKED FIRST, and a holder refuses regardless of
                # any error reported alongside it — an incomplete scan that DID
                # see a handle is still conclusive about that handle.
                #
                # Attempts are sized against a measured failure rate, not
                # picked: a single scan refused ~1 in 10 times on an idle box,
                # and a 3-attempt bound still failed during a full test suite
                # where subprocess churn is heavy. 5 leaves a residual that is
                # rare enough to be acceptable for a supplementary guard, and
                # the cost is ~1s per retry, only on the failing path.
                # Scan-mode seam. Default `auto` borrows the same authority as
                # before; the other modes exist so a test can drive a branch
                # explicitly instead of inheriting the host's sudoers (a suite
                # gated on the host's privilege environment passes where sudo
                # exists and silently stops exercising the guard where it does
                # not), and so an operator who has established a verified
                # offline boundary by other means can say so out loud rather
                # than being refused.
                #   auto  — uid 0, else `sudo -n`, else refuse (legacy behaviour)
                #   plain — unprivileged scan; refuses when the scan REPORTS an
                #           error, and refuses outright where procfs is mounted
                #           with hidepid (where it cannot see every PID at all)
                #   sudo  — require `sudo -n`; refuse if unavailable
                #   none  — skip the scan; AUTHORITATIVE-BOUNDARY ASSUMED
                _HOLDER_SCAN_MODE="${GENESIS_RESTORE_HOLDER_SCAN:-auto}"
                _HOLDER_ATTEMPTS=5
                _HOLDER_ATTEMPT=0
                _HOLDER_OUT=""
                _HOLDER_RC=1
                # /proc/<pid>/fd symlinks name the RESOLVED target (with
                # " (deleted)" appended once the file is unlinked), and -lname
                # matches its operand as a GLOB against that text. Three
                # consequences, each a reviewed defect in the prefix pattern
                # "$DB_FILE*" this replaces: a symlinked component in DB_FILE
                # never matches the resolved link text (the guard goes blind); a
                # glob metacharacter in the path matches the wrong files; and
                # the prefix also matched this script's own
                # .pre-restore.<epoch> safety copies, so a forensic tool
                # holding one refused a restore that copy cannot affect. Match
                # the RESOLVED path, glob-escaped, against exactly the live
                # artifacts — main, -wal, -shm — each also in its " (deleted)"
                # form, so an unlinked-but-still-open handle (the shape the
                # 2026-09-18 incident actually had) still refuses.
                _DB_SCAN_REAL=$(readlink -f -- "$DB_FILE" 2>/dev/null) \
                    || _DB_SCAN_REAL="$DB_FILE"
                _DB_SCAN_PAT=$(printf '%s' "$_DB_SCAN_REAL" | sed 's/[][\\*?]/\\&/g')
                if [ "$_HOLDER_SCAN_MODE" = "none" ]; then
                    # `log`, not `warn`: warn() appends to _FAILURES, which makes
                    # the whole restore exit non-zero. Skipping the scan is an
                    # explicit operator choice, not a failure of the run.
                    log "SQLite: live-handle scan SKIPPED (GENESIS_RESTORE_HOLDER_SCAN=none). Sound only when exclusion is established by other means (a verified offline boundary); this scan is a supplementary guard, never exclusion."
                    _HOLDER_RC=0
                    _HOLDER_ATTEMPT=0
                fi
                while [ "$_HOLDER_SCAN_MODE" != "none" ] \
                    && [ "$_HOLDER_ATTEMPT" -lt "$_HOLDER_ATTEMPTS" ]; do
                    _HOLDER_ATTEMPT=$((_HOLDER_ATTEMPT + 1))
                    _HOLDER_OUT=""
                    _HOLDER_RC=0
                    case "$_HOLDER_SCAN_MODE" in
                    plain)
                        # An unprivileged glob silently OMITS hidepid-hidden PID
                        # directories, and find then exits 0 over the visible
                        # subset — so "no error" is NOT "complete visibility" and
                        # the guard would pass with a holder it cannot see. Under
                        # hidepid this mode cannot deliver what it claims, so it
                        # refuses rather than returning a clean-looking result.
                        # Match the OPTION, never an enumerated value list:
                        # `hidepid=[12]` missed `hidepid=4` and the symbolic
                        # spellings (`noaccess`, `invisible`, `ptraceable`) the
                        # 5.8+ multi-instance procfs work introduced. Anything
                        # not explicitly 0/off refuses — same RULE as
                        # scripts/check_cc_running_versions.sh, which already
                        # learned this closed-set-of-values mistake. One
                        # deliberate divergence from its regex: only the LAST
                        # /proc mount line is evaluated, because /proc/mounts
                        # lists overmounts in order and an any-line match lets a
                        # stale `hidepid=off` entry beneath the effective
                        # hidepid mount stand the refusal down (measured in
                        # review; the mirror still has that hole — tracked as a
                        # follow-up issue).
                        _HP_LINE=""
                        _HP_LINE=$(grep -E '(^| )/proc proc ' /proc/mounts 2>/dev/null | tail -n 1) \
                            || _HP_LINE=""
                        if printf '%s' "$_HP_LINE" | grep -qE '(,| )hidepid=' \
                            && ! printf '%s' "$_HP_LINE" | grep -qE '(,| )hidepid=(0|off)(,| )'; then
                            _HOLDER_RC=2
                        else
                            _HOLDER_OUT=$(find /proc/[0-9]*/fd \( \
                                -lname "$_DB_SCAN_PAT" -o -lname "$_DB_SCAN_PAT (deleted)" \
                                -o -lname "$_DB_SCAN_PAT-wal" -o -lname "$_DB_SCAN_PAT-wal (deleted)" \
                                -o -lname "$_DB_SCAN_PAT-shm" -o -lname "$_DB_SCAN_PAT-shm (deleted)" \
                                -o -lname "$_DB_SCAN_PAT-journal" -o -lname "$_DB_SCAN_PAT-journal (deleted)" \
                                \) -print 2>/dev/null) \
                                || _HOLDER_RC=$?
                        fi
                        ;;
                    sudo)
                        # No `sudo -n true` pre-probe: sudo authorization is
                        # COMMAND-specific, so a least-privilege sudoers rule
                        # granting exactly the scan command below fails a `true`
                        # probe and the restore refused with precisely the
                        # authority it needed. The scan itself is the probe —
                        # an unauthorized sudo makes it exit non-zero, which is
                        # already the refusal path. Its stderr is suppressed so
                        # a persistent authorization failure does not print a
                        # password prompt error once per retry; the die message
                        # names the sudoers requirement instead.
                        #
                        # The glob MUST expand inside the privileged shell.
                        # `sudo find /proc/[0-9]*/fd ...` expands the bracket
                        # in the CALLER, before sudo starts — so under
                        # procfs `hidepid=2` the hidden PID directories are
                        # absent from find's operands, find succeeds over the
                        # visible subset, and the guard passes while an
                        # unseen holder exists. That is a fail-open in the
                        # exact direction this guard exists to prevent.
                        if command -v sudo >/dev/null 2>&1; then
                            _HOLDER_OUT=$(sudo -n sh -c \
                                'find /proc/[0-9]*/fd \( -lname "$1" -o -lname "$1 (deleted)" -o -lname "$1-wal" -o -lname "$1-wal (deleted)" -o -lname "$1-shm" -o -lname "$1-shm (deleted)" -o -lname "$1-journal" -o -lname "$1-journal (deleted)" \) -print 2>/dev/null' \
                                _ "$_DB_SCAN_PAT" 2>/dev/null) || _HOLDER_RC=$?
                        else
                            _HOLDER_RC=2
                        fi
                        ;;
                    auto)
                        if [ "$(id -u)" -eq 0 ]; then
                            _HOLDER_OUT=$(find /proc/[0-9]*/fd \( \
                                -lname "$_DB_SCAN_PAT" -o -lname "$_DB_SCAN_PAT (deleted)" \
                                -o -lname "$_DB_SCAN_PAT-wal" -o -lname "$_DB_SCAN_PAT-wal (deleted)" \
                                -o -lname "$_DB_SCAN_PAT-shm" -o -lname "$_DB_SCAN_PAT-shm (deleted)" \
                                -o -lname "$_DB_SCAN_PAT-journal" -o -lname "$_DB_SCAN_PAT-journal (deleted)" \
                                \) -print 2>/dev/null) \
                                || _HOLDER_RC=$?
                        elif command -v sudo >/dev/null 2>&1; then
                            # Glob inside the privileged shell — see the `sudo`
                            # branch above: expanding it in the caller omits
                            # hidepid-hidden PIDs and turns the guard fail-open.
                            # And no `sudo -n true` pre-probe, for the same
                            # reason as that branch: authorization is command-
                            # specific, so the scan itself is the probe. An
                            # unauthorized sudo exits non-zero here, exhausts
                            # the bounded retry loop, and refuses — the same
                            # terminal state the old pre-probe reached, without
                            # rejecting a least-privilege sudoers rule that
                            # grants exactly this scan.
                            _HOLDER_OUT=$(sudo -n sh -c \
                                'find /proc/[0-9]*/fd \( -lname "$1" -o -lname "$1 (deleted)" -o -lname "$1-wal" -o -lname "$1-wal (deleted)" -o -lname "$1-shm" -o -lname "$1-shm (deleted)" -o -lname "$1-journal" -o -lname "$1-journal (deleted)" \) -print 2>/dev/null' \
                                _ "$_DB_SCAN_PAT" 2>/dev/null) || _HOLDER_RC=$?
                        else
                            # No uid that can see every /proc/<pid>/fd, and no
                            # non-interactive elevation to borrow one. Retrying
                            # cannot fix this, but the retry loop is bounded and
                            # this branch simply exhausts it and refuses.
                            _HOLDER_RC=2
                        fi
                        ;;
                    *)
                        die "unknown GENESIS_RESTORE_HOLDER_SCAN='${_HOLDER_SCAN_MODE}' (expected auto|plain|sudo|none) — quarantine retained"
                        ;;
                    esac
                    if [ -n "$_HOLDER_OUT" ] || [ "$_HOLDER_RC" -eq 0 ]; then
                        break
                    fi
                    [ "$_HOLDER_ATTEMPT" -lt "$_HOLDER_ATTEMPTS" ] && sleep 1
                done
                if [ -n "$_HOLDER_OUT" ]; then
                    die "SQLite database still has open process handles after server stop — quarantine retained:
${_HOLDER_OUT}"
                fi
                if [ "$_HOLDER_RC" -ne 0 ]; then
                    die "SQLite holder inspection could NOT be completed conclusively after ${_HOLDER_ATTEMPT} attempt(s) (last rc=${_HOLDER_RC}) — refusing rather than assuming no holder exists. An unreadable /proc/<pid>/fd is an UNKNOWN holder, not an absent one. Resolve visibility (run this restore as a uid that can read every /proc/<pid>/fd, or grant non-interactive sudo for the scan — the privileged command is a \`sh -c 'find /proc/[0-9]*/fd ...'\` wrapper, so a sudoers rule must permit that sh invocation, not just \`find\`) or establish a verified offline boundary, then re-run. This scan is a supplementary guard, NOT exclusion: it cannot by itself prevent a new holder appearing between inspection and replacement. Quarantine retained."
                fi

                # `mv SOURCE DIR` moves the source INSIDE a directory and exits
                # 0, so a DB_FILE that resolves to a directory would swallow the
                # staged database, report a successful swap, and fail only at
                # final verification — with the staged file stranded inside the
                # directory. A directory here is a misconfiguration, never a
                # database; refuse before touching anything.
                if [ -d "$DB_FILE" ]; then
                    die "DB_FILE '$DB_FILE' is a directory — a rename onto it would move the staged database INSIDE it and report success. Fix the path before restoring. Quarantine retained."
                fi
                sync -f "$_DB_STAGE"
                _PRE_RESTORE=""
                _MOVED_WAL=false
                _MOVED_SHM=false
                _REMOVED_WAL=false
                _REMOVED_SHM=false
                if [ -f "$DB_FILE" ]; then
                    _PRE_RESTORE="${DB_FILE}.pre-restore.$(date +%s)"
                    # Hard-link the quiesced main file so DB_FILE remains valid
                    # until the candidate's single atomic rename.
                    ln "$DB_FILE" "$_PRE_RESTORE" \
                        || die "could not preserve pre-restore database — quarantine retained"
                fi
                # The sidecars are cleared REGARDLESS of whether a pre-restore
                # copy was taken. This is not a detail: a stale WAL surviving the
                # rename REPLAYS onto the restored database, replacing its pages
                # with the old database's — and it does so SILENTLY, because the
                # result is self-consistent, so the post-install integrity check
                # passes and the restore reports success. Sharing the
                # `[ -f "$DB_FILE" ]` guard above would skip this whole block
                # exactly when the main file is absent or is not a regular file,
                # which is the case that needs it most.
                #
                # With somewhere to move them to, they are RENAMED ASIDE:
                # a same-directory rename is atomic and preserves bytes exactly,
                # so the failure path needs no copy and no verification, and a
                # copy killed mid-write (measured: 1024 of 64189 bytes) can no
                # longer leave a TRUNCATED sidecar at the live path. With no
                # pre-restore copy there is nowhere to move them, and removing
                # them is the only way to stop the replay.
                for _sidecar in wal shm; do
                    # "Any pathname present", not "a regular file": `-f` is FALSE
                    # for a dangling symlink and for a directory, so a `-f` guard
                    # silently skips them where the base's unconditional `rm -f`
                    # cleared whatever was there. A left-behind dangling `-wal`
                    # symlink is not inert — SQLite cannot create the sidecar
                    # through a dangling target, so the restored service fails to
                    # open for writes while the read-only final check passes.
                    if [ ! -e "$DB_FILE-$_sidecar" ] && [ ! -L "$DB_FILE-$_sidecar" ]; then
                        continue
                    fi
                    if [ -n "$_PRE_RESTORE" ]; then
                        # A failed move must not exit before rolling back the ones
                        # already moved: the live main DB would be left present
                        # with its WAL/SHM gone — the de-fanged state this block
                        # exists to prevent, and the same class as the original
                        # defect, reintroduced in a different phase. A same-
                        # directory mv is a rename, so it either moved or it did
                        # not; there is no partial state to reason about.
                        if ! mv "$DB_FILE-$_sidecar" "${_PRE_RESTORE}-${_sidecar}" 2>/dev/null; then
                            # Say what actually happened: when the FIRST move is
                            # the one that failed, nothing was moved and nothing
                            # was "moved back" — a message claiming a rollback
                            # ran would misstate the on-disk state to the
                            # operator reading it mid-incident.
                            if ! $_MOVED_WAL && ! $_MOVED_SHM; then
                                die "could not move the live ${_sidecar} aside — nothing had been moved before it, so the live database and its sidecars are as they were${_PRE_RESTORE:+ (pre-restore hard link at ${_PRE_RESTORE} retained)}. Quarantine retained."
                            fi
                            _ROLLBACK_OK=true
                            if $_MOVED_WAL; then
                                mv "${_PRE_RESTORE}-wal" "$DB_FILE-wal" 2>/dev/null \
                                    || _ROLLBACK_OK=false
                            fi
                            if $_MOVED_SHM; then
                                mv "${_PRE_RESTORE}-shm" "$DB_FILE-shm" 2>/dev/null \
                                    || _ROLLBACK_OK=false
                            fi
                            if $_ROLLBACK_OK; then
                                die "could not move the live ${_sidecar} aside — the sidecars moved before it were moved back, so the live database is as it was. Quarantine retained."
                            fi
                            die "could not move the live ${_sidecar} aside AND a previously moved sidecar could not be moved back — the live DB is missing its WAL and/or SHM. Recover from ${_PRE_RESTORE}* before retrying. Quarantine retained."
                        fi
                        # Record the ACTION, not whether the destination now exists:
                        # a stale artifact left at that path by an earlier run that
                        # shared the same epoch second would otherwise read as "we
                        # moved this one" and be moved back in its place.
                        case "$_sidecar" in
                        wal) _MOVED_WAL=true ;;
                        shm) _MOVED_SHM=true ;;
                        esac
                    else
                        rm -f "$DB_FILE-$_sidecar" \
                            || die "could not remove the stale ${_sidecar} — quarantine retained"
                        case "$_sidecar" in
                        wal) _REMOVED_WAL=true ;;
                        shm) _REMOVED_SHM=true ;;
                        esac
                    fi
                done
                if [ -n "$_PRE_RESTORE" ]; then
                    _kept="DB"
                    $_MOVED_WAL && _kept="${_kept}, WAL"
                    $_MOVED_SHM && _kept="${_kept}, SHM"
                    log "SQLite: preserved raw pre-restore artifacts (${_kept}) → $_PRE_RESTORE*"
                fi
                if $_REMOVED_WAL || $_REMOVED_SHM; then
                    log "SQLite: no pre-restore main database (absent or not a regular file) — stale sidecars REMOVED, not moved aside, so a failed swap cannot move them back"
                fi
                # The sidecars were moved aside above, so the rename is the only
                # remaining mutation. On failure each sidecar that was moved is
                # moved BACK — a same-directory rename preserves bytes exactly,
                # so there is nothing to copy and nothing to verify. Tracking is
                # per-sidecar, so "there was nothing to restore" can never be
                # reported as "restored and verified".
                if ! mv "$_DB_STAGE" "$DB_FILE"; then
                    _TRIO_OK=true
                    if $_MOVED_WAL; then
                        mv "${_PRE_RESTORE}-wal" "$DB_FILE-wal" 2>/dev/null || _TRIO_OK=false
                    fi
                    if $_MOVED_SHM; then
                        mv "${_PRE_RESTORE}-shm" "$DB_FILE-shm" 2>/dev/null || _TRIO_OK=false
                    fi
                    if ! $_TRIO_OK; then
                        die "SQLite atomic replacement failed AND a sidecar could not be moved back — the live DB is missing its WAL and/or SHM. Recover from ${_PRE_RESTORE:-<no pre-restore copy was taken>} before retrying. Quarantine retained."
                    fi
                    if $_MOVED_WAL || $_MOVED_SHM; then
                        die "SQLite atomic replacement failed; the live DB and its sidecars were moved back into place unchanged — the live database is as it was. Quarantine retained."
                    fi
                    # Branch on what was RECORDED, not on the negation of the other
                    # pair: `!(MOVED_WAL || MOVED_SHM)` does not imply there was no
                    # pre-restore copy — a copy with no sidecars present leaves both
                    # flags false, and that is the ordinary clean-shutdown shape.
                    if $_REMOVED_WAL || $_REMOVED_SHM; then
                        die "SQLite atomic replacement failed; there was no pre-restore copy, so the live -wal/-shm were REMOVED and cannot be restored. The live main database is as it was (absent or not a regular file) and now has no sidecars. Quarantine retained."
                    fi
                    die "SQLite atomic replacement failed; nothing was moved aside and nothing was removed, so the live database and its sidecars are as they were${_PRE_RESTORE:+ (pre-restore copy at ${_PRE_RESTORE})}. Quarantine retained."
                fi
                _DB_STAGE=""
                sync -f "$DB_FILE"
                sync -f "$(dirname "$DB_FILE")"
                _FINAL_CHECK_OUTPUT=""
                if ! _FINAL_CHECK_OUTPUT=$( \
                    PYTHONPATH="$_SCRIPT_DIR/../src" python3 -m genesis.db.integrity check \
                        "$DB_FILE" --source restore-complete --quarantine-on-failure 2>&1
                ); then
                    # Explicit corruption already produced a marker. Operational
                    # or otherwise indeterminate failure deliberately did not;
                    # fence the installed inode without mislabelling it corrupt.
                    if ! PYTHONPATH="$_SCRIPT_DIR/../src" python3 -c '
import sys
from genesis.db.integrity import database_is_quarantined
raise SystemExit(0 if database_is_quarantined(sys.argv[1]) else 1)
' "$DB_FILE"; then
                        PYTHONPATH="$_SCRIPT_DIR/../src" python3 -m genesis.db.integrity mark \
                            "$DB_FILE" --source restore-final-verification-incomplete \
                            --detail "${_FINAL_CHECK_OUTPUT:-final integrity check failed without detail}" \
                            >/dev/null \
                            || die "installed database final verification failed and durable fence could not be established"
                    fi
                    die "installed database failed final verification — quarantine retained"
                fi
                _SQLITE_RESTORED=true
                log "SQLite: restored and verified → $DB_FILE"
            else
                die "SQLite: dump payload is empty — live database left untouched"
            fi
            rm -f "$_SQL_TMP"
        else
            log "SQLite: skipped (user declined)"
        fi
    fi
else
    log "SQLite: no backup payload found (neither genesis.sql.gpg nor genesis.sql)"
fi

if $DATABASE_ONLY; then
    if $DRY_RUN; then
        _SUCCESS=true
        log "Database-only restore dry-run complete"
        exit 0
    fi
    $_SQLITE_RESTORED || die "database-only restore did not install a database"
    if [ ${#_FAILURES[@]} -ne 0 ]; then
        exit 1
    fi
    _SUCCESS=true
    log "Database-only restore complete; genesis-server remains stopped for operator verification"
    exit 0
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

# ── 7b. Hook audit stores ───────────────────────────────────────────
# NEVER overwrite a live record, even under --force. Restoring is purely ADDITIVE
# and that is a property of the store's shape rather than a rule we enforce: each
# file is named from the writing instant plus pid, so a backup file and a live one
# cannot collide unless they ARE the same record. The explicit existence test below
# fills the gaps a rebuild left and cannot destroy anything a running install has
# written since — deliberately not `cp -n`, whose skip is indistinguishable from a
# copy in its exit status and which coreutils warns may change behaviour.
log "--- Hook audit stores ---"
_AUDIT_SRC="$BACKUP_DIR/audit/merge_overrides"
# ASK, do not assume — same resolver the writer and the pruner use. Restoring to
# the hardcoded default put an install with a custom GENESIS_MERGE_OVERRIDE_DIR
# back together with its audit trail in a directory nothing reads (Codex P2,
# PR #1609).
# The resolver reads the ENVIRONMENT, and a custom store is normally configured
# only in secrets.env — which this script restores, and which on the disaster this
# backup exists for does not exist until it has. So load it first, and note that
# this whole section runs AFTER "Secrets" for exactly that reason: resolving before
# then put the records in the default directory while the writers went on using the
# configured one, leaving the recovered audit trail orphaned (Codex P2, PR #1609).
if [ -f "$SECRETS_FILE" ] && [ -z "${GENESIS_MERGE_OVERRIDE_DIR:-}" ]; then
    # shellcheck source=scripts/lib/load_secrets.sh
    source "$_SCRIPT_DIR/lib/load_secrets.sh" 2>/dev/null || true
    if declare -F load_secrets_file >/dev/null 2>&1; then
        load_secrets_file "$SECRETS_FILE" || true
    fi
fi
_AUDIT_DST="$(python3 "$_SCRIPT_DIR/hooks/audit_jsonl.py" --store-dir GENESIS_MERGE_OVERRIDE_DIR 2>/dev/null \
    || printf '%s' "$HOME/.genesis/merge_overrides")"
if [ ! -d "$_AUDIT_SRC" ]; then
    log "Audit stores: no backup payload"
elif $DRY_RUN; then
    log "Audit stores: would restore $(find "$_AUDIT_SRC" -maxdepth 1 -type f -name '*.jsonl' 2>/dev/null | wc -l) file(s) → $_AUDIT_DST"
else
    mkdir -p "$_AUDIT_DST" && chmod 0700 "$_AUDIT_DST"
    _AUDIT_RESTORED=0
    while IFS= read -r -d '' _f; do
        # 0600 to match the writer's own guarantee; umask alone would not promise it.
        _dst="$_AUDIT_DST/$(basename "$_f")"
        # Count only REAL copies. `cp -n` exits 0 when it SKIPS an existing file, so
        # counting its status reported every skipped file as restored. Test the
        # destination's absence instead, which is the condition actually meant.
        if [ ! -e "$_dst" ]; then
            # Copy to a TEMP name and rename into place. A bare `cp` that fails
            # partway — a full disk is the realistic one — leaves a truncated
            # JSONL at the destination, adds no warning, and lets the restore
            # report success; every later restore then SKIPS that file because
            # `-e` is now true, even with --force, so the corruption is permanent
            # and silent (Codex P2, PR #1609). rename(2) is atomic within the
            # directory, so the destination either does not exist or is whole.
            _tmp="$_dst.partial.$$"
            if cp "$_f" "$_tmp" 2>/dev/null && mv -f "$_tmp" "$_dst" 2>/dev/null; then
                chmod 0600 "$_dst" 2>/dev/null || true
                _AUDIT_RESTORED=$(( _AUDIT_RESTORED + 1 ))
            else
                rm -f "$_tmp" 2>/dev/null || true
                warn "audit record $(basename "$_f") could not be restored"
            fi
        fi
    done < <(find "$_AUDIT_SRC" -maxdepth 1 -type f -name '*.jsonl' -print0 2>/dev/null)
    log "Audit stores: $_AUDIT_RESTORED file(s) restored → $_AUDIT_DST (existing left untouched)"
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
# A COMPLETE off-site snapshot can legitimately lack a Qdrant collection (the
# backup gates each collection on its being reachable+fresh that run — Qdrant is
# rebuildable from SQLite, so fresh-SQL-without-vectors beats no-snapshot). Warn
# loudly when we restored the DB but no vectors, so the operator rebuilds them
# instead of running on a silently-empty vector store.
if ! $DRY_RUN && $_SQLITE_RESTORED && [ "$_QDRANT_RESTORED" -eq 0 ]; then
    log "NOTE: SQLite restored but 0 Qdrant collections were in this snapshot."
    log "      Rebuild the vector store from the DB: python -m genesis reindex (or scripts/reindex_fts_to_qdrant.py)."
fi
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
