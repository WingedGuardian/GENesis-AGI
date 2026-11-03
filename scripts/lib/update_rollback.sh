# update_rollback.sh: what update.sh runs to undo a failed update, and the service
# and record helpers that undoing needs. Sourced by update.sh at startup. Moved
# verbatim from update.sh, in its order (#3844).
#
# Definitions only, apart from two lines that are safe to run again (a test holds
# the list to these two): SERVER_RESTART_OWED, a constant, and _DB_RESTORE_LANDED,
# which only the restore changes, so 0 is right until it runs. Run state lives in
# update.sh.
#
# Reads update.sh's globals when called, never when sourced.
# BEGIN restart-owed (extracted by tests/test_scripts/test_bootstrap_crash_restart.py)
# The restart an update owes genesis-server (#3451). update.sh writes it
# immediately before it stops the server (and refuses to stop the server when it
# cannot), and again before a rollback stops it; bootstrap's crash recovery writes
# it when a crashed update left the server stopped without one, as an update from
# before this file existed does.
#
# It is retired in exactly ONE way: update.sh's own health check of genesis-server
# passes, and the file is removed right then, before any later step (P6's health
# loop on the full activation, _server_health_ok on the no-delta path and on a
# rollback that verifies health). Nothing else pays it. Not the unit's activation
# time: genesis-server is Type=simple, so "active" is not "healthy", and a server
# that came up and died before answering is still down. Not a file's age either.
# A kill anywhere after the stop leaves it, a power loss included (which runs no
# handler), whatever state file that kill left and whether or not bootstrap's
# crash recovery ever reads one.
#
# While it exists, the next update restarts genesis-server and health-checks it,
# the "Already up to date" path included. A deliberate stay-down is honoured only
# by the operator's hold marker (#2718), which _server_recovery_pending checks
# first once it exists. Two removals are not payments, because no debt was ever
# incurred: the stop was refused before anything stopped (the record is false),
# and _ensure_server_down failed with the server still running. Nothing else
# consumes it: the alert record (last_update_failure.json) is separate, because
# the version collector archives that one.
SERVER_RESTART_OWED="$HOME/.genesis/server_restart_owed.json"
# Written DURABLY: a rename is atomic but not power-loss durable on its own, so
# the temporary file is fsynced before the rename and the directory after it
# (fsync(2); the same pattern as cc_version.sh's settings writer and
# codebase_managed.py's sync_staging). Only then may the server be stopped: any
# failure, including either fsync, returns 1 and the caller refuses the stop. A
# failure AFTER the rename (the directory fsync) leaves the new file in place, so
# a caller that refuses the stop removes what it wrote (see its call site).
_owe_server_restart() {
    mkdir -p "$HOME/.genesis" 2>/dev/null || true
    if ! python3 -I - "$SERVER_RESTART_OWED" "$$" "${STARTED_AT:-}" 2>/dev/null <<'PY'
import json
import os
import sys
from datetime import datetime

path, pid, started_at = sys.argv[1:]
tmp = path + ".tmp"
data = {
    "pid": int(pid),
    "started_at": started_at,
    "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
}
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
try:
    os.write(fd, (json.dumps(data) + "\n").encode())
    os.fsync(fd)
finally:
    os.close(fd)
os.replace(tmp, path)
dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
try:
    os.fsync(dfd)
finally:
    os.close(dfd)
PY
    then
        rm -f "$SERVER_RESTART_OWED.tmp" 2>/dev/null || true
        echo "  ERROR: could not write $SERVER_RESTART_OWED durably." >&2
        return 1
    fi
}
# Is genesis-server, which is NOT running, owed a restart? Prints why and returns
# 0; returns 1 when nothing says an update left it down, so it stays as the
# operator left it. The one predicate for recovery detection and the no-delta
# dispatch, so the two cannot disagree. It reads presence only, never a time:
# the restart record above, a leftover last_update_failure.json, or a last
# update_history status of rolled_back/failed (or one that cannot be read).
# (_latest_update_status is defined below.)
_server_recovery_pending() {
    # The operator hold marker (#2718: keep a stopped server stopped) goes HERE,
    # first, once it exists: it is the only way a deliberate stay-down outranks
    # the evidence below.
    if [ -e "$SERVER_RESTART_OWED" ]; then
        echo "an earlier update stopped genesis-server and never health-checked it back"
        return 0
    fi
    if [ -f "$HOME/.genesis/last_update_failure.json" ]; then
        echo "a prior update's failure record is present"
        return 0
    fi
    case "$(_latest_update_status)" in
        rolled_back | failed)
            echo "the last update did not complete cleanly"
            return 0
            ;;
        unreadable)
            # Unknown is not "nothing failed". Recovering costs a restart and a
            # health check (with rollback on failure); guessing "operator stop"
            # would record success over what may be a live failure.
            echo "the last update's status could not be read"
            return 0
            ;;
    esac
    return 1
}
# END restart-owed

# The marker half of _clear_deploy_state, alone: what a run releases when the
# state file is not its to delete.
_clear_deploy_marker() {
    local _m="${GENESIS_HOME:-$HOME/.genesis}/update_in_progress.pid" _pid
    _pid="$(cat "$_m" 2>/dev/null || true)"
    # "Dead" includes a zombie (_deploy_marker_holder_live, in deploy_marker.sh),
    # which would otherwise keep a stale marker until its parent reaps it.
    if [ "$_pid" = "$$" ] || { [ -n "$_pid" ] && ! _deploy_marker_holder_live "$_pid"; }; then
        rm -f "$_m" 2>/dev/null || true
    fi
}

# BEGIN live-helpers (extracted by tests/test_scripts/test_update_live.py)
# On `live` the recorded commit is the reviewed base under the rebuild's tip.
_live_new_commit() {
    NEW_TAG=$(git -C "$GENESIS_ROOT" describe --tags --match 'v*' --abbrev=0 "$DEPLOY_HEAD" 2>/dev/null || echo "untagged")
    NEW_COMMIT=$(git -C "$GENESIS_ROOT" rev-parse --short "$DEPLOY_HEAD")
}
# The engine's move record: "live_move" in this run's own state row ($STATE_FILE),
# which the engine writes before and after each move (#3492; deploy_candidates.py
# record_move). Prints "<to> <phase>" and returns 0; returns 2 when the row has no
# record (the engine never wrote one: it moved nothing), 1 when it cannot be read.
# Both successful adoption and rollback consume the same exact move identity.
_live_read_move_record() {
    python3 -I -S -c '
import json, re, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        state = json.load(fh)
except FileNotFoundError:
    raise SystemExit(2)
if not isinstance(state, dict):
    raise SystemExit(1)
if "live_move" not in state:
    raise SystemExit(2)
d = state["live_move"]
if not isinstance(d, dict):
    raise SystemExit(1)
to, phase = d.get("to"), d.get("phase")
if not (isinstance(to, str) and re.fullmatch("[0-9a-f]{40}(?:[0-9a-f]{24})?", to)
        and phase in ("ref", "moving", "partial", "clean", "done")):
    raise SystemExit(1)
print(to, phase)
' "$STATE_FILE" 2>/dev/null
}
# A rebuild that moved `live` and then failed, or was interrupted (the shell runs a
# trap only once the engine has exited), leaves the checkout at a tip this run made
# but never adopted. What the engine did is read from its move record, never
# inferred from git: the tip is taken as this run's own (and so moved back to the
# tag) only when the record names it and HEAD is on this run's branch at exactly
# that commit. Anything else stays "moved by someone else". Nothing from the moved
# tree runs here. The tree is suspect (the rollback then restarts nothing on it)
# when the record cannot be read, when it says git wrote part of the tree
# ("partial"), or when it says the switch began ("moving", or "ref") and the
# two-dot residue toward its target is non-empty: the paths a switch from the tag
# to it writes, the range update_state_guard.py judges a live row by. A switch
# that began and wrote nothing leaves the old tip as it was, and it restarts. The
# record stays in the row: if the rollback keeps the state, every later reader
# sees it.
_live_adopt_tip() {
    [ "${ON_LIVE:-0}" = 1 ] && [ "${_LIVE_ENGINE_RAN:-0}" = 1 ] || return 0
    local got="" to="" phase="" rc=0 rb="" residue="" rec
    got="$(_live_read_move_record)" || rc=$?
    [ "$rc" -ne 2 ] || return 0
    if [ "$rc" -eq 0 ] && [ -n "$got" ]; then
        to="${got% *}"
        phase="${got#* }"
        if genesis_checkout_unmoved "$GENESIS_ROOT" "$to" "$ORIGINAL_BRANCH"; then
            [ "$to" != "${UPDATE_OWN_HEAD:-}" ] || return 0
            echo "  The rebuild had moved live to ${to:0:12} before it stopped; rolling that back."
            UPDATE_OWN_HEAD="$to"
            return 0
        fi
    fi
    case "$phase" in
        moving|ref)
            rb="$(git -C "$GENESIS_ROOT" rev-parse -q --verify "$ROLLBACK_TAG^{commit}" 2>/dev/null || true)"
            if [ -n "$rb" ]; then
                # NUL-separated records, read as such (a $(...) would drop the
                # NULs), each shown as one line: the status, then the path as
                # one shell token (printf %q), so a newline or a control
                # character in a name never splits a line or reaches a terminal.
                local -a recs=()
                mapfile -d '' -t recs < <(genesis_merge_residue "$GENESIS_ROOT" "$rb" "$to" --two-dot)
                if ! wait $!; then
                    residue="(the residue check failed)"
                fi
                for rec in "${recs[@]}"; do
                    case "$rec" in
                        "("*) residue+="$rec"$'\n' ;;
                        *) residue+="${rec:0:3}$(printf '%q' "${rec:3}")"$'\n' ;;
                    esac
                done
            else
                residue="(the rollback tag cannot be resolved)"
            fi
            [ -n "$residue" ] || return 0
            ;;
        partial|"") ;;
        *) return 0 ;;
    esac
    _LIVE_TREE_SUSPECT=1
    echo "  The rebuild may have written part of the new tree (move record: ${phase:-unreadable}); HEAD is not at its target."
    [ -z "$residue" ] || printf '%s' "$residue" | sed 's/^/    /'
    return 0
}
# END live-helpers

# Works with both systemd and bare-process environments.
_stop_genesis_server() {
    # Try systemctl first (works when D-Bus session bus is available)
    if systemctl --user is-active --quiet genesis-server.service 2>/dev/null; then
        systemctl --user stop genesis-server.service 2>/dev/null && return 0
    fi
    # Fallback: read PID from fcntl lock file
    local lock_file="$HOME/.genesis/genesis-server.lock"
    if [ -f "$lock_file" ]; then
        local pid
        pid=$(tr -d '\0' < "$lock_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            # `|| true`: the process can exit between the kill -0 check above and
            # this signal (a race); a failed kill must not abort under set -e
            # (and, once the ERR trap is armed in P4b, must not trigger rollback).
            kill -TERM "$pid" 2>/dev/null || true
            # Wait up to 10s for graceful shutdown
            for i in $(seq 1 20); do
                kill -0 "$pid" 2>/dev/null || return 0
                sleep 0.5
            done
            kill -KILL "$pid" 2>/dev/null || true
            return 0
        fi
    fi
    # Last resort: pkill by command pattern
    pkill -TERM -f "python -m genesis serve" 2>/dev/null || true
    sleep 1
}

_ensure_server_down() {
    # Guarantee genesis-server is stopped AND stays stopped before mutating the
    # repo/DB. systemd's Restart=on-failure can resurrect the server after a
    # kill-based stop (the kill reads as a failure, arming a RestartSec timer); an
    # explicit `systemctl stop` transitions the unit to inactive/dead and DISARMS
    # that timer (on-failure only fires from active/running). Without this, an
    # auto-restarted STALE-code process runs during the merge + migration window —
    # the bug that shipped a deploy whose running process never loaded the new code.
    systemctl --user stop genesis-server.service 2>/dev/null || true
    for _ in $(seq 1 20); do
        if ! systemctl --user is-active --quiet genesis-server.service 2>/dev/null \
           && ! pgrep -f "python -m genesis serve" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.5
    done
    # The restart timer may have fired between the stop and the poll — stop again,
    # then hard-kill as a last resort.
    systemctl --user stop genesis-server.service 2>/dev/null || true
    pkill -KILL -f "python -m genesis serve" 2>/dev/null || true
    sleep 1
    if pgrep -f "python -m genesis serve" >/dev/null 2>&1; then
        echo "  ERROR: genesis-server could not be stopped"
        return 1
    fi
    return 0
}

_start_genesis_server() {
    # Use `restart` (NOT `start`): systemd's Restart=on-failure can resurrect a
    # STALE-code instance mid-update (a kill-based stop is seen as a failure and
    # arms a RestartSec timer). `start` is a no-op on that already-running instance
    # and would silently leave the OLD code live after the update. `restart` always
    # stop+starts from the current on-disk code, and still starts the unit cleanly
    # when it is already stopped.
    if systemctl --user restart genesis-server.service 2>&1; then
        echo "  Started genesis-server via systemd"
        return 0
    fi
    # Fallback: start directly — DEGRADED MODE
    # This bypasses systemd monitoring, so health dashboard will show red.
    echo "  WARNING: systemctl --user restart failed — falling back to direct start (degraded)"
    echo "  Health monitoring will not work correctly. Run: systemctl --user restart genesis-server.service"
    # Close the update-lock FD in this long-lived child: nohup'd, it OUTLIVES
    # update.sh, and an inherited lock FD would keep the advisory lock held after
    # we exit — deadlocking every future update until this degraded server dies.
    # (systemd-started servers don't inherit our FDs; only this nohup path does.)
    # The checkout-lock FD too, closed in the child's own subshell: it is unset
    # when the lock is not held, and a `{var}>&-` on an unset name fails the
    # command. The subshell execs, so $! is the server's pid.
    (
        if [ -n "${GENESIS_CHECKOUT_LOCK_FD:-}" ]; then exec {GENESIS_CHECKOUT_LOCK_FD}>&-; fi
        exec nohup "$VENV_DIR/bin/python" -m genesis serve --host 0.0.0.0 --port 5000 \
            {_UPDATE_LOCK_FD}>&-
    ) >> "$HOME/.genesis/logs/genesis-server.log" 2>&1 &
    # No unit tracks this process, so the no-change path's health probe waits on
    # this pid instead of the unit's state.
    _SERVER_DIRECT_PID=$!
    echo "  Started genesis-server in degraded mode (pid $_SERVER_DIRECT_PID)"
    # Write marker so dashboard can detect degraded mode
    echo "nohup" > "$HOME/.genesis/server-start-mode"
}

# BEGIN db-restore-owed (extracted by tests/test_scripts/test_update_db_restore_owed.py)
# Decides DB_RESTORE_OWED by MEASURING, at rollback, whether this update changed
# what the old code depends on. The restore source ($DB_FILE.pre-update.peer-restore)
# and the live database are compared on their DDL (sqlite_master), user_version
# and the set of applied numbered migrations (schema_migrations: a migration can
# rewrite data without touching DDL). Any difference, or any failure to compare,
# owes the restore. Identical means the old code is back on its own schema, so
# the database is kept with the writes made since the snapshot. Predicting this
# from the merged paths was tried first and measured wrong: the 2026-10-31
# update changed one comment in db/schema/ and left the schema identical.
# Not compared: data migrations (d-prefixed; they may also have changed Qdrant,
# which a DB restore would not undo) and value-only rows create_all_tables rewrites.
# System python3 with -I -S, because the venv may be what broke. Read-only
# (mode=ro, which still sees WAL content); the busy timeout bounds a lock wait,
# and the reads themselves take milliseconds.
# Usage: _measure_db_restore_owed <code_action> <server_down>, from _do_rollback.
#   none / untouched: the checkout is at the rollback tag, or this run never
#     merged and someone else moved it. This update's code never ran, so it owes
#     nothing; restoring under someone else's code would put that code on an
#     old schema.
#   server not confirmed down: a still-running server can change the database
#     after it is measured, so the restore is owed (and refused, which marks the
#     rollback incomplete).
#   no snapshot this run: the snapshot step skips only a database that did not
#     exist yet (it exits the update on any other failure), so one that exists
#     now was created by this update and owes a restore that cannot happen.
_measure_db_restore_owed() {
    # code_action is not consulted: a checkout still at the rollback tag (`none`)
    # does not mean the database was untouched. A --post-merge with no state file
    # tags HEAD itself, and a no-delta tier-2 activation runs bootstrap and the
    # migrations with HEAD at the tag; either can change the schema and fail.
    # What was done to the database is measured, not inferred from the code.
    # shellcheck disable=SC2034  # kept in the signature for the callers' reading
    local code_action="$1" server_down="$2" rc=0 out=""
    if [ "$server_down" != "true" ]; then
        DB_RESTORE_OWED=1
        echo "  The server is not confirmed down, so the database cannot be measured reliably."
        return 0
    fi
    if [ "${DB_SNAPSHOT_TAKEN:-0}" != "1" ]; then
        if [ -f "$DB_FILE" ]; then DB_RESTORE_OWED=1; else DB_RESTORE_OWED=0; fi
        return 0
    fi
    out="$(python3 -I -S - "$DB_FILE.pre-update.peer-restore" "$DB_FILE" 2>&1 <<'SCHEMA_PY'
import sqlite3
import sys
from pathlib import Path


def shape(path):
    # as_uri() percent-encodes: a raw "file:{path}" lets a '?' or '#' in the
    # install path cut the path short and drop mode=ro.
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        ddl = set(con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))
        if not ddl:
            # A Genesis database always has tables; an empty one is a mis-open
            # or a missing file SQLite created, never something to call identical.
            raise RuntimeError(f"no schema in {path}")
        version = con.execute("PRAGMA user_version").fetchone()[0]
        has = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        applied = {r[0] for r in con.execute("SELECT id FROM schema_migrations")} if has else set()
    finally:
        con.close()
    return ddl, version, applied


# Exit 0 = identical, 3 = differs, anything else = could not compare (an
# uncaught error exits 1, so "differs" must not share that code).
try:
    before, after = shape(sys.argv[1]), shape(sys.argv[2])
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}")
    sys.exit(2)
if before == after:
    sys.exit(0)
changed = sorted({row[1] for row in before[0] ^ after[0]})
migrations = before[2] ^ after[2]
print(
    f"{len(changed)} schema object(s) differ"
    + (f" ({', '.join(changed[:5])}{', ...' if len(changed) > 5 else ''})" if changed else "")
    + (f"; user_version {before[1]} -> {after[1]}" if before[1] != after[1] else "")
    + (f"; {len(migrations)} applied migration(s) differ" if migrations else "")
)
sys.exit(3)
SCHEMA_PY
)" || rc=$?
    case "$rc" in
        0) DB_RESTORE_OWED=0
           echo "  Database schema unchanged since the snapshot: the database is kept." ;;
        3) DB_RESTORE_OWED=1
           echo "  Database changed since the snapshot: $out" ;;
        *) DB_RESTORE_OWED=1
           echo "  Could not compare the database with its snapshot (exit $rc), so it is restored: $(printf '%s' "$out" | tail -1)" ;;
    esac
}
# END db-restore-owed

# BEGIN db-restore-through-sqlite (extracted by tests/test_scripts/test_update_db_restore_sqlite.py)
# Restores $DB_FILE.pre-update.peer-restore INTO the live database through
# SQLite's backup API, never by cp. Long-lived Claude Code MCP servers keep the
# live database open (a WAL-mode connection holds a SHARED lock for its whole
# life), so a cp under them left a rollback-journal file nobody could switch back
# to WAL ("database is locked" at the server's init) and left those readers
# serving pre-restore pages. Writing through SQLite's own locking instead lands
# with readers attached: they see the restored data on their next transaction,
# the file stays WAL, and nothing is terminated (MEASURED with idle readers, a
# reader mid-transaction, a writer holding the lock, and a 900 MB database).
# A backup killed before it commits leaves the live database exactly as it was
# (MEASURED: SIGTERM and SIGKILL mid-copy), so the timeout fails safe.
# The worker prints COMMITTED the moment the backup has landed, so a failure
# AFTER that point (verification, checkpoint) is reported as "restored, not
# verified" rather than "not restored". _DB_RESTORE_LANDED carries that to the
# rollback summary. The backup gets its own deadline (the bound minus 60 s:
# quick_check took 3 s on an 890 MB database), enforced through the progress
# callback, which fires on every BUSY retry.
_DB_RESTORE_LANDED=0
_restore_db_snapshot() {
    local limit="${GENESIS_UPDATE_DB_RESTORE_TIMEOUT_S:-600}" out="" rc=0
    _DB_RESTORE_LANDED=0
    if [[ ! "$limit" =~ ^[1-9][0-9]{0,6}$ ]] || [ "$limit" -le 60 ]; then
        echo "  Invalid GENESIS_UPDATE_DB_RESTORE_TIMEOUT_S (seconds above 60 required): the database is NOT restored."
        return 1
    fi
    out="$(timeout --kill-after=5s "$limit" python3 -I -S - "$DB_FILE.pre-update.peer-restore" "$DB_FILE" "$((limit - 60))" 2>&1 <<'RESTORE_PY'
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

started = time.monotonic()  # the backup deadline counts from here, not from the copy
source, live, backup_budget = sys.argv[1], sys.argv[2], int(sys.argv[3])


def fail(msg):
    # One printable line: error text can quote names from the snapshot's own
    # schema, and a newline there could forge a marker line bash trusts.
    msg = "".join(c if c.isprintable() else " " for c in str(msg))
    print(f"FAILED: {msg}", flush=True)
    sys.exit(2)


def shape(con):
    # Same comparison as the db-restore-owed worker above; keep the two in step.
    ddl = set(con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))
    has = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    applied = {r[0] for r in con.execute("SELECT id FROM schema_migrations")} if has else set()
    return ddl, con.execute("PRAGMA user_version").fetchone()[0], applied


try:
    # In WAL mode the whole snapshot is written to -wal before a checkpoint folds
    # it in, so it needs about one database of free space beside the live one.
    need = os.path.getsize(source) * 11 // 10 + (64 << 20)
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(live))).free
    if free < need:
        fail(f"{free >> 20} MiB free beside the database, {need >> 20} MiB needed")
    src = sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    # Check the snapshot BEFORE it overwrites anything: the backup copies pages
    # as they are, so a damaged snapshot would replace a consistent database.
    # quick_check returns one row, "ok", for a sound file and one row per problem
    # otherwise: the whole result is compared, not only its first row.
    if src.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        fail("the snapshot failed quick_check")
    # An empty or zero-byte file passes quick_check, and restoring it would empty
    # the live database; a Genesis database always has tables.
    if not shape(src)[0]:
        fail("the snapshot has no schema")
    dst = sqlite3.connect(live, timeout=30)
    for pragma in ("page_size", "encoding"):
        a = src.execute(f"PRAGMA {pragma}").fetchone()[0]
        b = dst.execute(f"PRAGMA {pragma}").fetchone()[0]
        if a != b:
            fail(f"{pragma} differs (snapshot {a}, live {b})")
    # WAL first: a rollback-journal live file under a reader's SHARED lock is the
    # 2026-10-31 lock-out, and a backup into it would retry until the timeout.
    if dst.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        try:
            switched = dst.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        except sqlite3.OperationalError:
            switched = False
        if not switched:
            fail("the live database could not be switched to WAL (another process holds it)")
    # busy_timeout 0 for the backup: with a busy handler, one backup step waits
    # INSIDE SQLite and can then copy and commit, so a deadline checked afterwards
    # would report a restore that landed as one that did not (MEASURED). At 0 every
    # wait is a BUSY return to Python's retry loop, which the deadline governs.
    dst.execute("PRAGMA busy_timeout=0")
    deadline = started + backup_budget

    def progress(status, remaining, total):
        # Abort only a step that is WAITING (SQLITE_BUSY 5 / SQLITE_LOCKED 6);
        # one that returned SQLITE_DONE has committed and must never be reported
        # as not restored. (remaining is 0 both before the first page and after
        # the last, so it cannot tell the two apart.)
        if status in (5, 6) and time.monotonic() > deadline:
            raise TimeoutError(f"backup still blocked after {backup_budget}s")

    src.backup(dst, pages=-1, progress=progress, sleep=0.25)
    print("COMMITTED", flush=True)
    if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        fail("quick_check of the restored database did not return ok")
    if shape(dst) != shape(src):
        fail("the restored schema does not match the snapshot")
    # From here only the checkpoint remains, which never changes the data: a
    # stop now is a verified restore whose WAL a later checkpoint folds in.
    print("VERIFIED", flush=True)
    # Still at busy_timeout 0 (set before the backup), which matters here too: a
    # TRUNCATE checkpoint waits for readers WHILE holding the writer lock, so any
    # wait would block every writer for its length (MEASURED 30 s at the default).
    # A reader still in a transaction just leaves the WAL to a later checkpoint.
    busy = dst.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    print("CHECKPOINT busy" if busy else "CHECKPOINT done", flush=True)
except SystemExit:
    raise
except Exception as exc:
    fail(f"{type(exc).__name__}: {exc}")
RESTORE_PY
)" || rc=$?
    # Whole-line matches: the output also carries error text, which may quote paths.
    local verified=0
    grep -qx COMMITTED <<<"$out" && _DB_RESTORE_LANDED=1
    grep -qx VERIFIED <<<"$out" && verified=1
    if [ "$rc" -ne 0 ] && [ "$verified" = 1 ]; then
        # Stopped during the checkpoint, after verification: the data is restored
        # and checked; the WAL is folded in by a later checkpoint.
        echo "  Restored and verified the pre-update database snapshot through SQLite (its WAL checkpoint was cut short; a later one folds it in)."
        return 0
    fi
    if [ "$rc" -eq 0 ]; then
        case "$out" in
            *"CHECKPOINT busy"*) echo "  Restored the pre-update database snapshot through SQLite (a reader still in a transaction keeps the WAL until a later checkpoint)." ;;
            *) echo "  Restored the pre-update database snapshot through SQLite." ;;
        esac
        return 0
    fi
    local why
    why="$(printf '%s\n' "$out" | grep '^FAILED: ' | tail -1)"
    [ -n "$why" ] || why="exit $rc$([ "$rc" -eq 124 ] || [ "$rc" -eq 137 ] && echo ", stopped at the ${limit}s bound")"
    if [ "$_DB_RESTORE_LANDED" = 1 ]; then
        echo "  CRITICAL: the pre-update database snapshot WAS restored, but its verification did not complete (${why#FAILED: }) — check the database before starting services."
    else
        echo "  CRITICAL: the pre-update database snapshot was NOT restored (${why#FAILED: }); the live database is unchanged — old code may run against a migrated schema."
    fi
    return 1
}
# END db-restore-through-sqlite

# Records an entry in update_history. Silently no-ops if the table
# doesn't exist yet (first update before migration 0001 has run).
_record_update_history() {
    local status="$1"           # success | failed | rolled_back | conflicts_pending
    local reason="${2:-}"
    local degraded="${3:-}"
    if [ -n "${PRE_UPDATE_DEGRADED:-}" ]; then
        degraded="${degraded:+$degraded,}$PRE_UPDATE_DEGRADED"
    fi
    # The pre-update package record is retired once the update or its rollback
    # has completed; one a failed rollback leaves is recovery material.
    case "$status" in
        success | rolled_back)
            if [ "${DEPS_STATE_TAKEN:-0}" = 1 ] && [ -n "${DEPS_STATE_FILE:-}" ]; then
                rm -rf "$DEPS_STATE_FILE" "${DEPS_STATE_FILE%.json}.lib"
            fi ;;
    esac
    local db_path="$GENESIS_ROOT/data/genesis.db"
    [ -f "$db_path" ] || return 0
    # The insert imports genesis (and through it aiosqlite), so ONLY the venv can
    # run it — a system interpreter lacks the dependencies. Without the venv the
    # record cannot be written; say so instead of skipping it silently.
    if [ ! -x "$VENV_DIR/bin/python" ]; then
        echo "  WARNING: failed to record update_history entry: no venv interpreter at $VENV_DIR/bin/python" >&2
        return 0
    fi

    # Run the insert in Python for parameterized SQL. The inline script
    # distinguishes three exit paths:
    #   0 — inserted OK
    #   2 — table missing (first update before 0001 ran) — expected, silent
    #   1 — any other error (logged to stderr + bash warns)
    # Do NOT pipe stderr to /dev/null — silencing failures is the
    # antipattern we're fixing. Only the "table missing" case is
    # allowed to be silent.
    # Note: we use `|| py_rc=$?` pattern because set -e otherwise triggers
    # on any non-zero $() assignment (including the expected rc=2 for
    # "table missing" case).
    local py_output=""
    local py_rc=0
    py_output=$(
        GH_STATUS="$status" \
        GH_REASON="$reason" \
        GH_DEGRADED="$degraded" \
        GH_DB_PATH="$db_path" \
        GH_OLD_TAG="$OLD_TAG" \
        GH_NEW_TAG="$NEW_TAG" \
        GH_OLD_COMMIT="$OLD_COMMIT" \
        GH_NEW_COMMIT="$NEW_COMMIT" \
        GH_ROLLBACK_TAG="$ROLLBACK_TAG" \
        GH_STARTED_AT="$STARTED_AT" \
        "$VENV_DIR/bin/python" - <<'PYEOF' 2>&1
import os
import sqlite3
import sys
import uuid
from datetime import UTC, datetime

from genesis.db.connection import connect_sqlite_rw

try:
    con = connect_sqlite_rw(os.environ["GH_DB_PATH"], timeout=5.0)
    con.execute(
        "INSERT INTO update_history "
        "(id, old_tag, new_tag, old_commit, new_commit, status, rollback_tag, "
        "failure_reason, degraded_subsystems, started_at, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            os.environ["GH_OLD_TAG"],
            os.environ["GH_NEW_TAG"],
            os.environ["GH_OLD_COMMIT"],
            os.environ["GH_NEW_COMMIT"],
            os.environ["GH_STATUS"],
            os.environ.get("GH_ROLLBACK_TAG") or None,
            os.environ.get("GH_REASON") or None,
            os.environ.get("GH_DEGRADED") or None,
            os.environ["GH_STARTED_AT"],
            datetime.now(UTC).isoformat(),
        ),
    )
    con.commit()
    con.close()
    sys.exit(0)
except sqlite3.OperationalError as exc:
    msg = str(exc).lower()
    if "no such table" in msg:
        # Expected on the very first update — 0001 hasn't run yet.
        sys.exit(2)
    print(f"update_history insert failed (OperationalError): {exc}", file=sys.stderr)
    sys.exit(1)
except Exception as exc:
    print(f"update_history insert failed ({type(exc).__name__}): {exc}", file=sys.stderr)
    sys.exit(1)
PYEOF
    ) || py_rc=$?
    case "$py_rc" in
        0) : ;;  # success
        2) : ;;  # table missing — expected
        *)
            echo "  WARNING: failed to record update_history entry:" >&2
            echo "    $py_output" >&2
            ;;
    esac
    return 0
}

# BEGIN deploy-outcome-probes (extracted by tests/test_scripts/test_update_success_records.py)
# Status of the most recent update_history row. ONE reader for the two places
# that must agree on it: P6's recovery detection, and the no-delta path, which
# must not write a newer success row over an unresolved `failed`/`rolled_back`
# one — when last_update_failure.json was never written, that row is the only
# signal recovery has.
#
# Prints the status; "" only when nothing is recorded (no database file, no
# table yet, no rows); and "unreadable" when a row may exist but could not be
# read. Callers treat "unreadable" as a possible unresolved failure, never as
# "nothing recorded". It reads the database _record_update_history writes
# ($GENESIS_ROOT, not a fixed home path), with any interpreter that can run
# stdlib sqlite3 — so a broken venv, which stops the WRITER, cannot also blind
# this reader to the row a previous run left.
_latest_update_status() {
    local db_path="$GENESIS_ROOT/data/genesis.db"
    if [ ! -f "$db_path" ]; then
        echo ""
        return 0
    fi
    local py=""
    py="$(_metadata_python 12 || true)"
    if [ -z "$py" ]; then
        echo "unreadable"
        return 0
    fi
    GH_DB_PATH="$db_path" "$py" - <<'PYEOF' 2>/dev/null || echo "unreadable"
import os
import sqlite3
import sys

try:
    # Read-only: never create or migrate the database from a status probe.
    con = sqlite3.connect(f"file:{os.environ['GH_DB_PATH']}?mode=ro", uri=True, timeout=5)
    try:
        # started_at is `date -Iseconds` (LOCAL time plus offset): order by the
        # instant, not the text, or a DST fall-back puts a later run behind an
        # earlier one.
        row = con.execute(
            "SELECT status FROM update_history ORDER BY datetime(started_at) DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        row = None
    print((row[0] or "") if row else "")
except Exception:
    sys.exit(1)
PYEOF
}

# Whether genesis-server answers its health endpoint right now. Used where a
# restart is attempted without P6's health loop: `_start_genesis_server` returns 0
# whenever `systemctl restart` exits cleanly, and its direct-start fallback
# returns 0 unconditionally, so its return code cannot say whether the server came
# back. Anything but a healthy answer counts as NOT up, so the error lands on the
# side that reports a problem rather than hiding one.
_server_health_ok() {
    # P6's probe (curl --max-time 20, above the health route's own ~15s budget),
    # retried every 15s ONLY while the unit says it is starting or running, for
    # at most 180s — P6's own floor for its health window. A unit reading
    # `active`/`activating` is never proof by itself: a crash-looping unit under
    # Restart=on-failure reads `activating` between attempts, so it waits out the
    # bound and reads "not back". An unreadable (empty) answer keeps polling to the
    # bound. Any other unit state (inactive, failed) stops at once, unless
    # _start_genesis_server fell back to a direct start: no unit tracks that
    # process, so the wait follows its pid instead. A server booting slower than
    # the bound reads "not back": the side that reports a problem. The bound is ELAPSED time ($SECONDS), so a transfer
    # that hangs for its full --max-time counts against it too; the worst overrun is
    # one poll step plus one transfer (the deadline is checked before the sleep).
    local state
    local deadline=$((SECONDS + ${_SERVER_HEALTH_WAIT_SECS:-180})) step="${_SERVER_HEALTH_POLL_SECS:-15}"
    while :; do
        curl -sf --max-time 20 http://localhost:5000/api/genesis/health > /dev/null 2>&1 && return 0
        state="$(systemctl --user is-active genesis-server.service 2>/dev/null || true)"
        case "$state" in
            active | activating | reloading) ;;
            "")
                # Unreadable (a lost user bus prints nothing and exits non-zero),
                # not evidence of death — P6 reads it the same way. Keep polling
                # to the bound; a direct-started process that is gone still stops it.
                if [ -n "${_SERVER_DIRECT_PID:-}" ]; then
                    kill -0 "$_SERVER_DIRECT_PID" 2>/dev/null || return 1
                fi
                ;;
            *)
                [ -n "${_SERVER_DIRECT_PID:-}" ] && kill -0 "$_SERVER_DIRECT_PID" 2>/dev/null \
                    || return 1
                ;;
        esac
        [ "$SECONDS" -lt "$deadline" ] || return 1
        sleep "$step"
    done
}
# END deploy-outcome-probes

_do_rollback() {
    local reason="$1"
    local degraded="${2:-}"
    # 1 = a restarted genesis-server must also answer its health endpoint before
    # the verdict counts the rollback complete. Only the live units refusal asks
    # (its exit 4 promises the old tip is back and up); every other caller passes
    # nothing (read as 0, so set -u never trips on it), and main's rollback never
    # waits on it.
    local verify_health="${3:-0}"

    # Disarm the ERR trap to prevent recursive rollback
    trap - ERR INT TERM
    # A rollback whose live hook restore or service restart failed is not clean
    # (_ROLLBACK_CLEAN), whatever the summary below prints. Only this rollback's
    # verdict sets it: exit 4's gate reads it.
    _ROLLBACK_CLEAN=0
    local hooks_ok=true svc_ok=true

    echo ""
    echo "  UPDATE FAILED — $reason"
    echo "  Rolling back to $ROLLBACK_TAG..."

    # Stop any running services first. _ensure_server_down also disarms the
    # on-failure restart timer so a stale instance can't come back mid-rollback
    # (best-effort here — the rollback continues even if it can't fully stop).
    # A server this rollback stops and means to restart is owed that restart
    # (restart-owed) from before the stop: the health check that passed earlier in
    # this run may already have retired the record, and a kill mid-rollback must
    # not leave the server down with nothing saying so. Best-effort: the rollback
    # goes on either way. A server this run never restarted (not in WERE_RUNNING)
    # stays as the operator left it, so it gets no record.
    local server_down=true
    if [[ " ${WERE_RUNNING[*]:-} " == *" genesis-server "* ]]; then
        _owe_server_restart \
            || echo "  WARNING: could not record the restart this rollback owes genesis-server" >&2
    fi
    _stop_genesis_server
    if ! _ensure_server_down; then
        echo "  WARNING: genesis-server may still be running during rollback"
        server_down=false
    fi
    systemctl --user stop genesis-bridge 2>/dev/null || true
    if [ "${_CHECKOUT_LOCK_BUSY:-0}" != "1" ]; then
        if ! genesis_checkout_lock "$GENESIS_ROOT"; then
            echo "  WARNING: checkout lock busy during rollback; the checkout will be left as it is" >&2
        fi
    fi
    # On `live`, a rebuild that moved the checkout before it failed or was
    # interrupted: its tip is this run's to move back (live-helpers).
    _live_adopt_tip

    # What the code rollback may touch is decided from the checkout as it is NOW,
    # by exact commit identity: another session may have switched the branch,
    # committed, or edited files while this update ran, and a blind checkout plus
    # `reset --hard` would destroy that work.
    #   - on ORIGINAL_BRANCH at the rollback commit: this run never moved HEAD
    #     (a refusal or failure before the merge). Nothing to undo; no reset, so
    #     any edit made meanwhile is kept. One repair: a merge that was attempted
    #     and interrupted can leave a half-written tree with HEAD unchanged; a
    #     MERGE_HEAD there is aborted, and any other tracked change is reported,
    #     never reset (it may be someone else's edit).
    #   - on ORIGINAL_BRANCH at UPDATE_OWN_HEAD (this run's merge result): undo it
    #     by switching ORIGINAL_BRANCH back to the rollback tag with a non-forced
    #     checkout, which keeps every edit or refuses (the `reset` action).
    #   - moved, but this run never merged (UPDATE_OWN_HEAD is still the rollback
    #     commit): someone else moved it and this run changed no code. Leave it.
    #   - anything else: the checkout moved after this run's merge. Leave it
    #     exactly as it is, restart the services, and report the rollback
    #     incomplete for a person to sort out.
    # BEGIN rollback-code-guard (extracted by tests/test_scripts/test_update_activation.py)
    local checkout_ok=true code_action=moved rb_commit="" own_head="${UPDATE_OWN_HEAD:-}" left=""
    # True when this run's merged code stays on disk (the rollback's checkout was
    # refused, failed, or not attempted): the migrated database must then stay
    # with it.
    local code_kept=false
    rb_commit="$(git -C "$GENESIS_ROOT" rev-parse -q --verify "$ROLLBACK_TAG^{commit}" 2>/dev/null || true)"
    # The first call always runs, so _now_head/_now_branch describe the checkout
    # in the message below. An empty commit never matches (the predicate refuses it).
    if genesis_checkout_unmoved "$GENESIS_ROOT" "$rb_commit" "$ORIGINAL_BRANCH"; then
        code_action=none
    elif [ -n "$rb_commit" ] && genesis_checkout_unmoved "$GENESIS_ROOT" "$own_head" "$ORIGINAL_BRANCH"; then
        code_action=reset
    elif [ -n "$rb_commit" ] && [ "$own_head" = "$rb_commit" ]; then
        code_action=untouched
    fi
    # Without the checkout lock a Claude launch may be reading the tree, so
    # nothing here changes it: no switch back, no reset, no merge abort.
    # GENESIS_CHECKOUT_LOCK_FD is set only while the lock is held.
    local rollback_locked=true
    [ -n "${GENESIS_CHECKOUT_LOCK_FD:-}" ] || rollback_locked=false
    if [ "$rollback_locked" != "true" ] && [ "$code_action" != "none" ]; then
        code_action=locked_out
    fi
    # Someone SWITCHED branches (rather than committing on ours): if our branch
    # still points where this run left it and nothing uncommitted is in the way,
    # switch back with a NON-forced checkout. git refuses rather than overwrite a
    # tracked edit or an untracked file, and --no-overwrite-ignore makes it refuse
    # rather than overwrite an IGNORED file too, which a plain checkout replaces
    # without asking (both measured, git 2.43). The other branch keeps its
    # commits. Then decide again from the original branch.
    local ob_tip=""
    if { [ "$code_action" = "untouched" ] || [ "$code_action" = "moved" ]; } \
        && [ -n "$rb_commit" ] && [ "$_now_branch" != "$ORIGINAL_BRANCH" ]; then
        ob_tip="$(git -C "$GENESIS_ROOT" rev-parse -q --verify "refs/heads/$ORIGINAL_BRANCH^{commit}" 2>/dev/null || true)"
        if { [ "$ob_tip" = "$rb_commit" ] || [ "$ob_tip" = "$own_head" ]; } \
            && left="$(genesis_tracked_dirty_paths "$GENESIS_ROOT")" && [ -z "$left" ] \
            && genesis_without_checkout_lock git -C "$GENESIS_ROOT" checkout -q --no-overwrite-ignore "$ORIGINAL_BRANCH" 2>&1; then
            echo "  The checkout had been switched to ${_now_branch:-a detached HEAD}; switched back to $ORIGINAL_BRANCH (that branch keeps its commits)."
            if genesis_checkout_unmoved "$GENESIS_ROOT" "$rb_commit" "$ORIGINAL_BRANCH"; then
                code_action=none
            elif genesis_checkout_unmoved "$GENESIS_ROOT" "$own_head" "$ORIGINAL_BRANCH"; then
                code_action=reset
            fi
        else
            echo "  Did not switch back to $ORIGINAL_BRANCH (uncommitted tracked edits, $ORIGINAL_BRANCH moved on, or git refused to overwrite a file); left as found."
        fi
    fi
    case "$code_action" in
        none)
            echo "  The checkout is still at $ROLLBACK_TAG on $ORIGINAL_BRANCH: no code to roll back."
            if [ "${MERGE_ATTEMPTED:-0}" = "1" ]; then
                if git -C "$GENESIS_ROOT" rev-parse -q --verify MERGE_HEAD >/dev/null 2>&1 \
                    && [ "$rollback_locked" != "true" ]; then
                    echo "  CRITICAL: an interrupted merge is in progress, and without the checkout lock it is left for you to abort."
                    checkout_ok=false
                elif git -C "$GENESIS_ROOT" rev-parse -q --verify MERGE_HEAD >/dev/null 2>&1; then
                    if ! genesis_without_checkout_lock git -C "$GENESIS_ROOT" merge --abort 2>&1; then
                        echo "  CRITICAL: an interrupted merge is in progress and could not be aborted."
                        checkout_ok=false
                    fi
                elif ! left="$(genesis_tracked_dirty_paths "$GENESIS_ROOT")" || [ -n "$left" ]; then
                    echo "  CRITICAL: the merge was interrupted and tracked files differ from $ROLLBACK_TAG; they are left as they are (they may be someone else's edits):"
                    printf '%s\n' "${left:-(status unreadable)}" | sed 's/^/    /'
                    checkout_ok=false
                fi
            fi
            ;;
        untouched)
            echo "  This run never changed the code. The checkout was moved by someone else (now ${_now_branch:-a detached HEAD} at ${_now_head:0:12}) and is left as it is."
            checkout_ok=false
            ;;
        reset)
            # The measured non-forced rollback checkout lives in
            # scripts/lib/deploy_recovery.sh (genesis_rollback_checkout).
            if ! genesis_rollback_checkout "$GENESIS_ROOT" "$ROLLBACK_TAG" "$ORIGINAL_BRANCH" "$EPHEMERAL_BACKUP_ROOT" "$rb_commit"; then
                checkout_ok=false
                code_kept=true
            elif [ "${ON_LIVE:-0}" = 1 ]; then
                # The checkout ran with hooks off, so the git hooks the rebuild
                # installed are still the tip's; the engine puts back the old
                # tip's (readiness refuses every later rebuild until it does).
                if ! genesis_without_checkout_lock env DEPLOY_CANDIDATES_LOCK_FD="$_UPDATE_LOCK_FD" DEPLOY_CANDIDATES_EPHEMERAL_RE="$EPHEMERAL_DIRTY_RE" python3 -I -S "$SCRIPT_DIR/deploy_candidates.py" restore-hooks "$own_head" "$ROLLBACK_TAG"; then
                    hooks_ok=false
                    echo "  WARNING: could not restore the git hooks of $ROLLBACK_TAG; readiness refuses every rebuild until each installed hook matches scripts/hooks/ there."
                fi
            fi
            ;;
        locked_out)
            echo "  CRITICAL: the checkout lock could not be taken (a Claude launch holds genesis-checkout.lock), so the code was NOT rolled back: the checkout is left as found (${_now_branch:-a detached HEAD} at ${_now_head:0:12}). This update's merge is ${own_head:0:12}; the pre-update state is $ROLLBACK_TAG."
            checkout_ok=false
            code_kept=true
            ;;
        *)
            echo "  CRITICAL: the checkout moved after this update merged (now ${_now_branch:-a detached HEAD} at ${_now_head:0:12}); expected $ORIGINAL_BRANCH at ${own_head:0:12}."
            echo "  The code was NOT rolled back, so nobody's work is overwritten. This update's merge is ${own_head:0:12}; the pre-update state is $ROLLBACK_TAG. Undo the merge by hand once you know whose changes sit on top of it."
            checkout_ok=false
            ;;
    esac
    # Services come back ONLY on the code they were running before the update:
    # the original branch at the rollback commit, with no foreign tracked edit on
    # top. Anything else is code nobody validated, so nothing is reinstalled or
    # restarted from it, and the rollback is reported incomplete. (The watchdog
    # can still restart a stopped server from the checkout after the rollback ends;
    # a durable hold it honours is not built yet.)
    local restart_ok=false left_rc=0
    left=""
    if genesis_checkout_unmoved "$GENESIS_ROOT" "$rb_commit" "$ORIGINAL_BRANCH"; then
        left="$(genesis_tracked_dirty_paths "$GENESIS_ROOT")" || left_rc=$?
        [ "$left_rc" -eq 0 ] && [ -z "$left" ] && restart_ok=true
    fi
    if [ "$restart_ok" != "true" ]; then
        echo "  CRITICAL: the checkout is not the pre-update code ($ROLLBACK_TAG on $ORIGINAL_BRANCH, unmodified), so services are NOT restarted from it."
        if [ "$left_rc" -ne 0 ]; then
            echo "    (working-tree status unreadable)"
        elif [ -n "$left" ]; then
            printf '%s\n' "$left" | sed 's/^/    changed: /'
            genesis_has_flagged_entries "$GENESIS_ROOT" && echo "    (A listed path git status does not show is flagged: see 'git ls-files -v'; clear it with git update-index --no-assume-unchanged or --no-skip-worktree.)"
        fi
        checkout_ok=false
    fi
    if [ "${_LIVE_TREE_SUSPECT:-0}" = 1 ]; then
        restart_ok=false
        checkout_ok=false
        echo "  CRITICAL: services are NOT restarted: the rebuild may have left a mixed working tree (part of the new tree written while HEAD stayed on the old one). See git status in $GENESIS_ROOT, and the rebuild's own message above, for which paths changed."
    fi
    # END rollback-code-guard

    # BEGIN rollback-deps (extracted by tests/test_scripts/test_update_deps_restore.py)
    # Put the packages back to the pre-update record when one was taken: the
    # exact set, not a fresh resolve of the old checkout's ranges. Without a
    # record, the old re-resolve, now with an import check, since pip's status
    # alone once reported success over a server that could not import FastMCP.
    # A failed exact restore has already removed what the update added, so it is
    # followed by that re-resolve as salvage (the rollback still reports
    # incomplete): the services restart onto the best set available, not a
    # half-restored one. The browser-stack lock is waited on for 10 minutes here,
    # not bootstrap's 2 hours: the services are stopped while it waits.
    local pip_ok=true _deps_rc=0
    if [ "$restart_ok" = "true" ]; then
        if [ "${DEPS_STATE_TAKEN:-0}" = 1 ]; then
            GENESIS_BROWSER_STACK_WAIT_S=600 \
                restore_venv_state "$GENESIS_ROOT" "$VENV_DIR" "$DEPS_STATE_FILE" || _deps_rc=$?
            if [ "$_deps_rc" = 0 ]; then
                echo "  Restored the pre-update packages."
            elif [ "$_deps_rc" = 75 ]; then
                echo "  CRITICAL: packages not restored: a browser provisioning run held its lock for 600 s; nothing was changed (record kept: $DEPS_STATE_FILE)"
                pip_ok=false
            else
                echo "  CRITICAL: the pre-update packages were not restored (record kept: $DEPS_STATE_FILE)"
                pip_ok=false
                if "$VENV_DIR/bin/pip" install -e "$GENESIS_ROOT" --quiet 2>&1 | tail -1 \
                    && "$VENV_DIR/bin/python" -c "from genesis.runtime import GenesisRuntime; import genesis.mcp.health" 2>/dev/null; then
                    echo "  Salvage: re-resolved the old version ranges; Genesis imports, but the set is not the recorded one."
                else
                    echo "  Salvage: re-resolving the old version ranges did not give an importable set either."
                fi
            fi
        elif ! "$VENV_DIR/bin/pip" install -e "$GENESIS_ROOT" --quiet 2>&1 | tail -1 \
            || ! "$VENV_DIR/bin/python" -c "from genesis.runtime import GenesisRuntime; import genesis.mcp.health" 2>/dev/null; then
            echo "  CRITICAL: the packages could not be reinstalled for the rolled-back code"
            pip_ok=false
        fi
    fi
    # END rollback-deps

    # Restore the pre-update DB — but ONLY if this update may have changed its
    # schema (DB_RESTORE_OWED). Rolling back the code without the DB would leave
    # the old code running against a newer schema. Otherwise the DB is kept, with
    # the writes made since the snapshot, which a restore would discard.
    _measure_db_restore_owed "$code_action" "$server_down"
    # BEGIN rollback-db-decision (extracted by tests/test_scripts/test_update_checkout_moved.py)
    local db_ok=true
    if [ "${DB_RESTORE_OWED:-0}" = "1" ]; then
        if [ "$code_action" = "untouched" ]; then
            # Someone else moved the checkout and the rollback leaves their code in
            # place; the change measured may be theirs. Restoring the snapshot
            # under their code would delete their schema changes and writes.
            echo "  CRITICAL: the database changed and the checkout belongs to someone else (left in place) — the database is kept."
            db_ok=false
        elif [ "$code_action" = "moved" ] || [ "$code_kept" = "true" ]; then
            # The code was left as it is (merged, plus whatever moved it), so the
            # database it may have changed is the one that matches it. Restoring
            # the old DB here would put the new code on the old schema.
            echo "  CRITICAL: the schema may have changed and the code was not rolled back — the database is kept to match it."
            db_ok=false
        elif [ "${DB_SNAPSHOT_TAKEN:-0}" != "1" ]; then
            # A restore is owed but THIS run took no valid snapshot (the `.backup`
            # failed, or only a stale prior-run file exists). We cannot roll the
            # schema back, so the rollback is NOT clean — the old code will run
            # against a migrated DB. Flag it loudly for manual intervention rather
            # than report success or restore a stale snapshot.
            echo "  CRITICAL: a DB restore is owed but no current DB snapshot exists — cannot restore; old code may run against a changed schema"
            db_ok=false
        elif [ "$server_down" != "true" ]; then
            # Refuse to replace a possibly-live database: a server still running
            # the new code would keep writing to the restored one. Leave the
            # migrated DB in place (schema mismatch, but recoverable) and flag it.
            echo "  WARNING: server not confirmed down — SKIPPING DB restore (won't overwrite a live DB)."
            db_ok=false
        elif _restore_db_snapshot; then
            # Through SQLite, never cp: Claude Code MCP servers hold the live
            # database open whether or not the server is down (see the block).
            :
        else
            db_ok=false
        fi
    fi
    # END rollback-db-decision

    # Reload systemd in case unit templates changed during the failed update's
    # bootstrap. NOTE: rollback restores CODE, DEPS, and (if migrated) the DB —
    # it does NOT re-render installed systemd units or hooks; those self-heal on
    # the next successful update. daemon-reload picks up any unit-file drift left
    # on disk so the restart below uses a consistent unit.
    systemctl --user daemon-reload 2>/dev/null || true

    # Restart only when both old code and its database were restored. External
    # watchdog recovery remains separate; this rollback cannot certify a failure.
    genesis_checkout_unlock
    if [ "$restart_ok" = "true" ] && [ "$db_ok" = "true" ]; then
        for svc in "${WERE_RUNNING[@]}"; do
            if [ "$svc" = "genesis-server" ]; then
                if ! _start_genesis_server; then
                    svc_ok=false
                    echo "  CRITICAL: failed to restart genesis-server"
                fi
            elif ! systemctl --user start "$svc.service" 2>/dev/null; then
                svc_ok=false
                echo "  CRITICAL: failed to restart $svc"
            fi
        done
    elif [ "${#WERE_RUNNING[@]}" -gt 0 ]; then
        echo "  NOT restarted: ${WERE_RUNNING[*]}. Verify the checkout and database recovery before starting them."
    fi

    # ONE verdict for every report of this rollback: the message, the history
    # row, the failure file, the closing summary and exit 4's gate. A rollback
    # whose hooks or services did not come back is not complete, whatever the code
    # and database did.
    local rollback_ok=false
    if [ "$checkout_ok" = "true" ] && [ "$pip_ok" = "true" ] && [ "$db_ok" = "true" ] \
        && [ "$hooks_ok" = true ] && [ "$svc_ok" = true ]; then
        rollback_ok=true
    fi
    # _start_genesis_server's fallback returns 0 whether or not the server came
    # back, so when asked, health is part of this verdict, decided before anything
    # is recorded. Asked only of a rollback that is otherwise complete: an
    # incomplete one is already reported so, and never pays the wait. The traps
    # are already disarmed: a TERM during this wait (up to _server_health_ok's
    # 180 s bound) ends the run with no final history row (the run's open row
    # stays unfinished) and no failure file, and keeps the state file (the clear
    # below never runs), which is fail-safe: nothing claims the rollback succeeded.
    if [ "$rollback_ok" = true ] && [ "$verify_health" = 1 ] && [ "$restart_ok" = "true" ] \
        && [[ " ${WERE_RUNNING[*]} " == *" genesis-server "* ]]; then
        if _server_health_ok; then
            # update.sh's own health check passed: the restart owed (restart-owed)
            # is paid, now, before anything else runs.
            rm -f "$SERVER_RESTART_OWED"
        else
            rollback_ok=false
            echo "  CRITICAL: genesis-server was restarted but did not answer its health endpoint"
        fi
    fi
    if [ "$rollback_ok" = true ]; then
        echo "  Rolled back to $ROLLBACK_TAG (code, deps$([ "${DB_RESTORE_OWED:-0}" = "1" ] && echo ", database"))."
        echo "  NOT reverted: installed systemd units and hooks (self-heal on the next update)."
        _ROLLBACK_CLEAN=1
        _record_update_history "rolled_back" "$reason" "$degraded"
    else
        echo "  ROLLBACK INCOMPLETE — manual intervention required"
        echo "  Last known good state: $OLD_TAG ($OLD_COMMIT) on $ORIGINAL_BRANCH"
        # BEGIN rollback-db-summary (extracted by tests/test_scripts/test_update_db_restore_sqlite.py)
        if [ "$db_ok" != "true" ] && [ "$code_action" != "moved" ] \
            && [ "$code_action" != "untouched" ] && [ "$code_kept" != "true" ]; then
            if [ "$_DB_RESTORE_LANDED" = 1 ]; then
                echo "  DB restored but NOT verified — check it before starting services."
            else
                echo "  DB restore FAILED — old code may be running against a migrated schema."
            fi
        fi
        # END rollback-db-summary
        _record_update_history "failed" "$reason (rollback incomplete)" "$degraded"
    fi

    echo ""
    echo "  To diagnose: discuss with Claude Code"
    echo "  Context: Update from $OLD_TAG to $NEW_TAG failed."
    echo "  Reason: $reason"
    [ -n "$degraded" ] && echo "  Degraded subsystems: $degraded"

    # Write failure context for CC to pick up.
    # Values passed as positional args so json.dump() handles all escaping —
    # raw git output in $reason can contain quotes and backslashes.
    mkdir -p "$HOME/.genesis"
    python3 -c "
import json, sys
data = {
    'old_tag':             sys.argv[1],
    'new_tag':             sys.argv[2],
    'old_commit':          sys.argv[3],
    'new_commit':          sys.argv[4],
    'rollback_tag':        sys.argv[5],
    'reason':              sys.argv[6],
    'degraded_subsystems': sys.argv[7],
    'original_branch':     sys.argv[8],
    'rollback_complete':   sys.argv[9] == 'true',
    'timestamp':           sys.argv[10],
}
with open(sys.argv[11], 'w') as f:
    json.dump(data, f, indent=4)
" "$OLD_TAG" "$NEW_TAG" "$OLD_COMMIT" "$NEW_COMMIT" "$ROLLBACK_TAG" \
  "$reason" "${degraded:-}" "$ORIGINAL_BRANCH" \
  "$rollback_ok" \
  "$(date -Iseconds)" \
  "$HOME/.genesis/last_update_failure.json"

    # Clear the in-progress signal files (mirrors the success-path cleanup) so a
    # leftover entry can't suppress the watchdog's deploy-restart guard after a
    # rollback. The server is back up (above); once this invocation exits its PID
    # dies anyway, but removing the files closes the PID-reuse window proactively.
    # BEGIN rollback-state-clear (extracted by tests/test_scripts/test_update_prior_state_guard.py)
    # The state file goes only when the code is back at the rollback tag
    # (checkout_ok): a rollback that left merged code on disk keeps it, because
    # it is what stops deploy_code_only.sh and the live engine restarting onto
    # that code. The same holds for a --post-merge, whose state it is finishing.
    # A kept state is described by the shared guard's own words, which name the
    # right remedy for the tree as it is (#3492, #3532): finish with
    # --post-merge, put back a half-written checkout first, or Dismiss and run
    # the update again. The line below is only for a guard that could not say.
    _kept_msg="" _kept_rc=0
    if [ "$checkout_ok" = "true" ]; then
        rm -f "$STATE_FILE" 2>/dev/null || true
    else
        _kept_msg="$(python3 -I -S "${SCRIPT_DIR:-}/lib/update_state_guard.py" "$STATE_FILE" "${GENESIS_ROOT:-}" 2>&1)" \
            || _kept_rc=$?
        case "$_kept_rc" in 0|1|3|4|6) ;; *) _kept_msg="" ;; esac
        if [ "$_kept_rc" -ne 10 ] && [ -n "$_kept_msg" ]; then
            echo "  $STATE_FILE is kept as the restart guard."
            printf '%s\n' "$_kept_msg" | sed 's/^/  /'
        elif [ "$_kept_rc" -eq 10 ]; then
            echo "  $STATE_FILE is kept, though the restart guard says it guards nothing now (nothing merged is on disk); Dismiss it on the dashboard's update card."
        elif [ "${_LIVE_TREE_SUSPECT:-0}" = 1 ]; then
            echo "  $STATE_FILE is kept as the restart guard: the rebuild may have left part of its checkout on disk. scripts/deploy_candidates repair-move puts it back to $ROLLBACK_TAG (it saves every path it changes first, and says when there is nothing to repair); by hand, put back the paths the rebuild's message above names (save any edits of yours first), then Dismiss it on the dashboard's update card."
        else
            echo "  $STATE_FILE is kept as the restart guard; the check that names its remedy could not run. Check the checkout by hand, then Dismiss it on the dashboard's update card."
        fi
    fi
    _clear_deploy_marker
    # END rollback-state-clear

    echo ""
    echo "  ──────────────────────────────────────"
    if [ "$rollback_ok" = true ]; then
        echo "  Rolled back: $OLD_TAG ($OLD_COMMIT) on $ORIGINAL_BRANCH"
    else
        echo "  NOT fully rolled back — see CRITICAL above. Pre-update state: $OLD_TAG ($OLD_COMMIT) on $ORIGINAL_BRANCH"
    fi
    echo ""
}
