# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# Pluggable Tier-2 (off-site) backup backend interface — sourced by
# scripts/backup.sh and scripts/restore.sh.
#
# WHY: the off-site destination must be SELECTABLE, not hard-coded to SMB/NAS.
# The public repo offers a MENU (none/local/smb; rclone lands in a follow-on PR)
# and each install picks one in secrets.env — no prescribed provider. backup.sh
# writes a dated snapshot tree (Genesis/<host>/<UTC-stamp>/{data,qdrant,transcripts}/
# plus a COMPLETE marker) and restore.sh pulls the latest COMPLETE one; BOTH talk
# to this interface instead of a backend binary directly.
#
# SELECTOR:  GENESIS_BACKUP_TIER2_BACKEND ∈ {none, local, smb}
#   Backward-compat: if it is UNSET but GENESIS_BACKUP_NAS is set, the backend
#   resolves to `smb` (existing NAS installs keep working untouched).
#
# PER-BACKEND CONFIG (env):
#   smb:   GENESIS_BACKUP_NAS=//host/share  GENESIS_BACKUP_NAS_USER  GENESIS_BACKUP_NAS_PASS
#   local: GENESIS_BACKUP_LOCAL_PATH=/mnt/somewhere  (a mounted dir / other-host fs)
#   none:  no off-site copy (local staging only)
#
# INTERFACE — every <remote-*> path is RELATIVE to the backend root:
#   backend_mkdir  <remote-dir>              create dir + parents (idempotent)
#   backend_put    <local-file> <remote-path>
#   backend_get    <remote-path> <local-file>
#   backend_list   <remote-dir>              emit child NAMES (files+dirs), one/line
#   backend_list_dirs <remote-dir>           emit child DIRECTORY names only, one/line
#   backend_exists <remote-path>             exit 0 if present, else 1
#   backend_delete <remote-path-or-dir>      recursive delete
#   backend_available                        exit 0 if the backend tool/config is usable
#
# LIFECYCLE:
#   backend_init     resolve backend + stage creds; call once, early.
#   backend_cleanup  remove transient creds; call from the caller's OWN EXIT handler.
#                    The lib registers NO trap of its own — backup.sh/restore.sh
#                    already own `trap … EXIT`, and a competing EXIT trap would
#                    silently replace theirs.
#
# NOTE on tier2_status: the CALLER maps backend outcomes to the EXISTING
# tier2_status value strings (ok/partial/not_configured/no_smbclient) — those are
# read by the dashboard + health alerts (src/genesis/mcp/health/errors.py), so the
# strings are deliberately unchanged here.

# Resolved state (set by backend_init).
_BACKEND=""             # resolved backend name: none|local|smb
_BACKEND_CREDS=""       # smb: temp creds file (cleaned by backend_cleanup)
_BACKEND_NAS=""         # smb: //host/share
_BACKEND_LOCAL_ROOT=""  # local: root directory

# ── Operation timeouts ───────────────────────────────────────────────
# Named failure mode: a hung SMB/NFS mount (or dead smbclient TCP session)
# blocks forever inside a backend op. Since SF5 both scripts hold the shared
# backup-restore flock for their whole run, an unbounded hang here would hold
# the DR lock indefinitely — blocking a real disaster-recovery restore. Every
# backend op is therefore bounded, in three tiers sized to the work:
#   ctl  (60s):   mkdir/list/exists — session setup + one dir op; seconds on
#                 any live link, 60s is 10-20x headroom.
#   xfer (1800s): put/get — the largest real payload is the episodic_memory
#                 Qdrant snapshot (~282MB and growing); at a worst-case
#                 0.3MB/s that is ~940s, so 900 would kill a slow-but-alive
#                 link. 1800 bounds a hung mount at 30min without capping a
#                 healthy slow transfer.
#   del  (600s):  recursive delete — O(files) server-side (a dated snapshot
#                 holds 1000+ transcript files); per-file SMB round trips can
#                 legitimately exceed the ctl tier.
# Env-overridable for unusual links (levers, not hardcoded policy).
_BACKEND_CTL_TIMEOUT="${GENESIS_BACKUP_CTL_TIMEOUT:-60}"
_BACKEND_XFER_TIMEOUT="${GENESIS_BACKUP_XFER_TIMEOUT:-1800}"
_BACKEND_DEL_TIMEOUT="${GENESIS_BACKUP_DEL_TIMEOUT:-600}"
_t_ctl()  { timeout "$_BACKEND_CTL_TIMEOUT"  "$@"; }
_t_xfer() { timeout "$_BACKEND_XFER_TIMEOUT" "$@"; }
_t_del()  { timeout "$_BACKEND_DEL_TIMEOUT"  "$@"; }

_backend_resolve() {
    local b="${GENESIS_BACKUP_TIER2_BACKEND:-}"
    # Backward-compat: a configured NAS with no explicit selector means smb.
    if [ -z "$b" ] && [ -n "${GENESIS_BACKUP_NAS:-}" ]; then b="smb"; fi
    printf '%s' "${b:-none}"
}

backend_init() {
    _BACKEND="$(_backend_resolve)"
    case "$_BACKEND" in
        smb)
            _BACKEND_NAS="${GENESIS_BACKUP_NAS:-}"
            # Creds in a temp file (not on the command line — avoids /proc/*/cmdline
            # + ps exposure). The CALLER removes it via backend_cleanup in its EXIT.
            _BACKEND_CREDS="$(mktemp)"
            chmod 600 "$_BACKEND_CREDS"
            printf 'username=%s\npassword=%s\n' \
                "${GENESIS_BACKUP_NAS_USER:-}" "${GENESIS_BACKUP_NAS_PASS:-}" > "$_BACKEND_CREDS"
            ;;
        local)
            _BACKEND_LOCAL_ROOT="${GENESIS_BACKUP_LOCAL_PATH:-}"
            ;;
        *) : ;;  # none / unknown: nothing to stage
    esac
}

backend_cleanup() {
    [ -n "$_BACKEND_CREDS" ] && rm -f "$_BACKEND_CREDS"
    _BACKEND_CREDS=""
}

# Name of the resolved backend (none|local|smb). For status/logging.
backend_name() { printf '%s' "$_BACKEND"; }

# 0 if the backend's tool + config are usable; non-zero otherwise.
# The local arm's dir probe is external `test` under timeout, NOT the `[ -d ]`
# builtin: a hung NFS/CIFS mount hangs the stat() inside the builtin with no
# way to bound it — and this probe runs FIRST, while the DR lock is held.
backend_available() {
    case "$_BACKEND" in
        smb)   [ -n "$_BACKEND_NAS" ] && command -v smbclient >/dev/null 2>&1 ;;
        local) [ -n "$_BACKEND_LOCAL_ROOT" ] && _t_ctl test -d "$_BACKEND_LOCAL_ROOT" ;;
        *)     return 1 ;;
    esac
}

# ── smb backend ──────────────────────────────────────────────────────
# _SMB_OP_TIMEOUT is dynamically scoped: put/get/delete override it per-call
# (bash `local` in the caller is visible here); everything else gets ctl.
_smb_run() { timeout "${_SMB_OP_TIMEOUT:-$_BACKEND_CTL_TIMEOUT}" smbclient "$_BACKEND_NAS" -A "$_BACKEND_CREDS" "$@"; }

_smb_mkdir() {
    # smbclient mkdir is non-recursive — create each ancestor in one -c batch.
    local dir="$1" acc="" cmd="" part
    local IFS='/'
    for part in $dir; do
        [ -n "$part" ] || continue
        acc="${acc:+$acc/}$part"
        cmd="${cmd}mkdir \"$acc\"; "
    done
    [ -n "$cmd" ] || return 0
    # Pre-existing levels fail harmlessly; the dir-tree is best-effort.
    _smb_run -c "$cmd" >/dev/null 2>&1 || true
}

_smb_put() {
    local src="$1" dst="$2" _SMB_OP_TIMEOUT="$_BACKEND_XFER_TIMEOUT"
    _smb_run -c "cd \"$(dirname "$dst")\"; put \"$src\" \"$(basename "$dst")\"" >/dev/null 2>&1
}

_smb_get() {
    local rem="$1" dst="$2" _SMB_OP_TIMEOUT="$_BACKEND_XFER_TIMEOUT"
    _smb_run -c "cd \"$(dirname "$rem")\"; get \"$(basename "$rem")\" \"$dst\"" >/dev/null 2>&1
}

_smb_list() {
    # Emit child names, one per line. Real entries carry an attribute column
    # (D/A/H/S/R/N); the trailing "NNN blocks of size ..." summary and ./.. are
    # excluded. (Names with spaces are not produced by our snapshot layout.)
    _smb_run -c "cd \"$1\"; ls" 2>/dev/null \
        | awk '$1!="." && $1!=".." && $2 ~ /^[DAHSRN]+$/ {print $1}' || true
}

_smb_list_dirs() {
    # Like _smb_list but DIRECTORIES only — a directory's attribute column contains
    # D (e.g. "D", "DH"), a file's never does. Used for host/stamp discovery so a
    # stray file under Genesis/ can't be mistaken for a host dir (would corrupt the
    # sole-host auto-detect on a fresh DR box).
    _smb_run -c "cd \"$1\"; ls" 2>/dev/null \
        | awk '$1!="." && $1!=".." && $2 ~ /D/ {print $1}' || true
}

_smb_exists() {
    local p="$1" b
    b="$(basename "$p")"
    _smb_run -c "cd \"$(dirname "$p")\"; ls \"$b\"" 2>/dev/null \
        | awk -v n="$b" '$1==n && $2 ~ /^[DAHSRN]+$/ {f=1} END{exit !f}'
}

_smb_delete() { local _SMB_OP_TIMEOUT="$_BACKEND_DEL_TIMEOUT"; _smb_run -c "deltree \"$1\"" >/dev/null 2>&1; }

# ── local backend (cp/ls/test/rm to a mounted path) ──────────────────
# Also the real-filesystem regression anchor for tests (no binary stub needed).
# Every op runs under a tiered timeout: "local" here usually means a MOUNTED
# path (NFS/CIFS), where a dead server hangs mkdir/cp/ls/stat forever — and
# builtins (`[ -e ]`) can't be bounded, hence external `test`.
_local_mkdir()  { _t_ctl mkdir -p "$_BACKEND_LOCAL_ROOT/$1" 2>/dev/null; }
_local_put()    { _t_ctl mkdir -p "$(dirname "$_BACKEND_LOCAL_ROOT/$2")" 2>/dev/null && _t_xfer cp "$1" "$_BACKEND_LOCAL_ROOT/$2"; }
_local_get()    { _t_xfer cp "$_BACKEND_LOCAL_ROOT/$1" "$2"; }
_local_list()   { _t_ctl ls -1A "$_BACKEND_LOCAL_ROOT/$1" 2>/dev/null || true; }
_local_list_dirs() { _t_ctl find "$_BACKEND_LOCAL_ROOT/$1" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' 2>/dev/null || true; }
_local_exists() { _t_ctl test -e "$_BACKEND_LOCAL_ROOT/$1"; }
_local_delete() { _t_del rm -rf "${_BACKEND_LOCAL_ROOT:?}/$1"; }

# ── dispatch ─────────────────────────────────────────────────────────
backend_mkdir() {
    case "$_BACKEND" in
        smb)   _smb_mkdir "$1" ;;
        local) _local_mkdir "$1" ;;
        *)     return 0 ;;
    esac
}
backend_put() {
    case "$_BACKEND" in
        smb)   _smb_put "$1" "$2" ;;
        local) _local_put "$1" "$2" ;;
        *)     return 1 ;;
    esac
}
backend_get() {
    case "$_BACKEND" in
        smb)   _smb_get "$1" "$2" ;;
        local) _local_get "$1" "$2" ;;
        *)     return 1 ;;
    esac
}
backend_list() {
    case "$_BACKEND" in
        smb)   _smb_list "$1" ;;
        local) _local_list "$1" ;;
        *)     return 0 ;;
    esac
}
backend_list_dirs() {
    case "$_BACKEND" in
        smb)   _smb_list_dirs "$1" ;;
        local) _local_list_dirs "$1" ;;
        *)     return 0 ;;
    esac
}
backend_exists() {
    case "$_BACKEND" in
        smb)   _smb_exists "$1" ;;
        local) _local_exists "$1" ;;
        *)     return 1 ;;
    esac
}
backend_delete() {
    case "$_BACKEND" in
        smb)   _smb_delete "$1" ;;
        local) _local_delete "$1" ;;
        *)     return 0 ;;
    esac
}
