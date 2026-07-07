#!/usr/bin/env bash
#
# One-time OFFLINE conversion of the Genesis SQLite DB to btrfs nodatacow (+C).
#
# WHY
#   On btrfs a copy-on-write SQLite DB suffers WAL write-amplification and
#   chronic fragmentation. Fresh installs get nodatacow via bootstrap.sh
#   (`chattr +C` on data/, inherited at file creation). An EXISTING install's
#   live genesis.db predates that flag, and `chattr +C` silently no-ops on a
#   non-empty file — the only way to apply nodatacow to existing data is to
#   rewrite it into a +C directory, which produces a NEW inode.
#
# WHY IT MUST BE OFFLINE (read this before running)
#   genesis.db is held open not only by genesis-server but by the MCP servers
#   of every running Claude Code session. Swapping the inode while any of them
#   hold it strands those connections on the old (unlinked) inode — split-brain
#   writes and data loss. This script therefore REFUSES to run unless it is the
#   SOLE holder of the DB after genesis-server is stopped. Close ALL Claude Code
#   sessions first, then run this from a PLAIN terminal — never from inside a CC
#   session, whose own MCP servers pin the DB.
#
# SAFETY
#   - Idempotent: exits 0 if already nodatacow, or if data/ is not btrfs.
#   - Pauses the host guardian across the downtime (writes paused.json, which
#     the guardian reads as a fallback when the server API is down) so it does
#     not try to "recover" the deliberately-stopped server. Prior pause state is
#     restored on exit.
#   - Backs up the DB before touching it; verifies integrity of the rewritten
#     copy BEFORE the swap and again after; rolls back from the backup on any
#     failure and restarts the server.
#
# USAGE
#   scripts/convert_db_nodatacow.sh --check    # report state + DB holders, no changes
#   scripts/convert_db_nodatacow.sh            # perform the conversion (asks nothing)
#
set -euo pipefail

GENESIS_ROOT="${GENESIS_ROOT:-$HOME/genesis}"
DB="${GENESIS_DB:-$GENESIS_ROOT/data/genesis.db}"
DATA_DIR="$(dirname "$DB")"
PAUSE_FILE="${GENESIS_PAUSE_FILE:-$HOME/.genesis/paused.json}"
SERVICE="genesis-server"

TS="$(date -u +%Y%m%d-%H%M%S)"
NOCOW_TMP="$DATA_DIR/.genesis.db.nocow.$$"
BAK="$DATA_DIR/genesis.db.pre-nocow-$TS.bak"

log() { printf '[nodatacow] %s\n' "$*"; }
die() { printf '[nodatacow] ERROR: %s\n' "$*" >&2; exit 1; }

# List PIDs (one per line) holding the DB, its -wal or its -shm open.
db_holders() {
  local fd tgt pid
  for fd in /proc/*/fd/*; do
    tgt="$(readlink "$fd" 2>/dev/null || true)"
    case "$tgt" in
      "$DB" | "$DB"-wal | "$DB"-shm)
        pid="$(printf '%s' "$fd" | cut -d/ -f3)"
        printf '%s\n' "$pid"
        ;;
    esac
  done 2>/dev/null | sort -un
}

holder_report() {
  local pid
  while read -r pid; do
    [ -n "$pid" ] || continue
    printf '    PID %s: %s\n' "$pid" \
      "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | cut -c1-90)"
  done
}

has_nocow() { # $1 = path; true if the +C (No_COW) flag is set
  lsattr -d "$1" 2>/dev/null | awk '{print $1}' | grep -q C
}

is_btrfs() { [ "$(stat -f -c %T "$DATA_DIR" 2>/dev/null || echo unknown)" = "btrfs" ]; }

# ---- preconditions ---------------------------------------------------------
[ -f "$DB" ]                 || die "DB not found: $DB"
command -v sqlite3 >/dev/null || die "sqlite3 not found"
command -v chattr  >/dev/null || die "chattr not found (install e2fsprogs)"
command -v lsattr  >/dev/null || die "lsattr not found (install e2fsprogs)"

if ! is_btrfs; then
  log "data dir is $(stat -f -c %T "$DATA_DIR" 2>/dev/null), not btrfs — nodatacow is btrfs-only. Nothing to do."
  exit 0
fi

# ---- --check mode (read-only) ---------------------------------------------
if [ "${1:-}" = "--check" ]; then
  log "data dir:        $DATA_DIR (btrfs)"
  log "genesis.db +C:   $(has_nocow "$DB" && echo yes || echo NO)"
  log "data dir +C:     $(has_nocow "$DATA_DIR" && echo yes || echo no)"
  holders="$(db_holders)"
  if [ -z "$holders" ]; then
    log "current DB holders: none"
  else
    log "current DB holders ($(printf '%s\n' "$holders" | grep -c .)):"
    printf '%s\n' "$holders" | holder_report
    log "NOTE: to convert, all of these except genesis-server must be gone"
    log "      (close every Claude Code session, then run without --check)."
  fi
  exit 0
fi

if has_nocow "$DB"; then
  log "genesis.db already has nodatacow (+C). Ensuring data dir also has +C."
  chattr +C "$DATA_DIR" 2>/dev/null || true
  log "Nothing else to do."
  exit 0
fi

# ---- guardian pause (restored on exit) ------------------------------------
PAUSE_BACKED_UP=0
pause_guardian() {
  mkdir -p "$(dirname "$PAUSE_FILE")"
  if [ -f "$PAUSE_FILE" ]; then cp -a "$PAUSE_FILE" "$PAUSE_FILE.mnt-bak"; PAUSE_BACKED_UP=1; fi
  printf '{"paused": true, "reason": "nodatacow DB conversion (maintenance)", "since": "%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PAUSE_FILE"
  log "guardian paused via $PAUSE_FILE"
}
resume_guardian() {
  if [ "$PAUSE_BACKED_UP" = "1" ] && [ -f "$PAUSE_FILE.mnt-bak" ]; then
    mv -f "$PAUSE_FILE.mnt-bak" "$PAUSE_FILE"
  else
    rm -f "$PAUSE_FILE"
  fi
  log "guardian pause state restored"
}

# ---- rollback trap ---------------------------------------------------------
STAGE=start
cleanup() {
  local rc=$?
  set +e
  rm -f "$NOCOW_TMP" "$NOCOW_TMP"-wal "$NOCOW_TMP"-shm
  if [ "$rc" -ne 0 ]; then
    log "FAILED at stage '$STAGE' (rc=$rc)."
    # Only a failure AT/AFTER the inode swap can leave a bad genesis.db; a
    # pre-swap failure leaves the original DB untouched, and a restart-only
    # failure leaves a good, already-verified DB — neither should be reverted.
    case "$STAGE" in
      swap | verify)
        if [ -f "$BAK" ]; then
          log "restoring DB from backup $BAK"
          cp -a "$BAK" "$DB"
          rm -f "$DB"-wal "$DB"-shm
        fi
        ;;
    esac
  fi
  # Always bring the server back and restore the guardian pause state.
  systemctl --user start "$SERVICE" 2>/dev/null || true
  resume_guardian 2>/dev/null || true
  if [ "$rc" -eq 0 ]; then
    log "DONE — genesis.db is now nodatacow. Backup retained at: $BAK"
  else
    log "Aborted/rolled back. Server restarted, guardian resumed."
    [ -f "$BAK" ] && log "Backup retained at: $BAK"
  fi
}
trap cleanup EXIT

# ---- perform the conversion ------------------------------------------------
log "converting $DB to nodatacow (btrfs +C)"
pause_guardian

STAGE=stop-server
log "stopping $SERVICE"
systemctl --user stop "$SERVICE"
sleep 2

STAGE=exclusive-check
holders="$(db_holders)"
if [ -n "$holders" ]; then
  log "REFUSING to proceed — the DB is still held open by:"
  printf '%s\n' "$holders" | holder_report
  die "close ALL Claude Code sessions (their MCP servers pin the DB) and re-run from a plain terminal"
fi
log "exclusive access confirmed (no other DB holders)"

STAGE=checkpoint
# Fold any committed-but-uncheckpointed WAL frames into the main DB so the
# backup is self-contained. Without this, an unclean stop (systemd escalating
# to SIGKILL past TimeoutStopSec) can leave dirty WAL frames that a plain
# `cp genesis.db` would miss — and PRAGMA integrity_check on the copy would
# still pass, so a rollback could silently discard committed data.
log "checkpointing WAL into the main DB"
sqlite3 "$DB" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null
if [ -s "$DB"-wal ]; then
  die "WAL not fully checkpointed ($(stat -c %s "$DB"-wal) bytes remain) — aborting"
fi

STAGE=backup
log "backing up DB to $BAK"
cp -a "$DB" "$BAK"
sqlite3 "$BAK" "PRAGMA integrity_check;" | grep -qx ok || die "backup failed integrity_check"

STAGE=dir-nocow
chattr +C "$DATA_DIR"
has_nocow "$DATA_DIR" || die "chattr +C did not take on $DATA_DIR"

STAGE=vacuum-into
log "VACUUM INTO fresh nodatacow file (compacts + defrags)"
rm -f "$NOCOW_TMP" "$NOCOW_TMP"-wal "$NOCOW_TMP"-shm
sqlite3 "$DB" "VACUUM INTO '$NOCOW_TMP';"
has_nocow "$NOCOW_TMP" || die "rewritten DB did not inherit +C — aborting before swap"
sqlite3 "$NOCOW_TMP" "PRAGMA integrity_check;" | grep -qx ok || die "rewritten DB failed integrity_check"

STAGE=reverify-exclusive
# The upfront guard is a snapshot, not a lock — a new CC session's MCP server
# (same user, direct SQLite connection) could have attached during backup/
# VACUUM. Re-check right before the destructive swap; a holder here would open
# the pre-swap inode and lose its writes when we unlink it below. Abort instead.
holders="$(db_holders)"
if [ -n "$holders" ]; then
  log "a new process attached to the DB mid-conversion:"
  printf '%s\n' "$holders" | holder_report
  die "aborting before swap to avoid data loss — close ALL Claude Code sessions and re-run"
fi

STAGE=swap
log "swapping in the nodatacow DB"
mv -f "$NOCOW_TMP" "$DB"          # rename within data/: keeps the +C inode
rm -f "$DB"-wal "$DB"-shm          # stale WAL/SHM from the old inode

STAGE=verify
has_nocow "$DB" || die "post-swap genesis.db lacks +C"
sqlite3 "$DB" "PRAGMA integrity_check;" | grep -qx ok || die "post-swap integrity_check failed"
log "verified: genesis.db is nodatacow and integrity_check ok"

STAGE=restart
systemctl --user start "$SERVICE"
sleep 3
systemctl --user is-active "$SERVICE" >/dev/null || die "$SERVICE did not come back active"
log "$SERVICE restarted"

STAGE=complete
exit 0
