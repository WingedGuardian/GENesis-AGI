"""SQLite lock-failure context and later kernel lock-table evidence.

``SerializedConnection._retry_locked`` logs ``DB lock persisted through N
attempts … giving up`` when a lock outlives its retry budget. That line says a
lock was held, never by whom. This module records local connection context
when capture is scheduled, then samples the kernel lock table (``/proc/locks``)
on a worker. The two readings are not atomic: a writer can release its lock or
a different writer can acquire one before the kernel sample. The JSON file
named by the warning supplies evidence, not proof of which connection caused
the original failure. ``taken_at`` timestamps the later kernel sample.

What SQLite's locks look like there (WAL mode, unix VFS, POSIX advisory locks;
MEASURED on a live install with two processes on a scratch database):

  - the ``-shm`` file carries the WAL locks at fixed byte offsets: 120 is the
    write lock (held WRITE by the connection inside ``BEGIN IMMEDIATE`` or a
    write transaction), 121 the checkpointer, 122 recovery, 123-127 the five
    reader marks, 128 the "DMS" byte every open connection holds READ;
  - the main database file carries a READ range from the pending/shared-lock
    region (1073741826-1073742335) for each open connection;
  - the device column (``00:39``) did NOT match ``os.stat().st_dev`` for the
    same file on that install, so files are matched by INODE alone and a
    mismatch is recorded as ``device_mismatch`` rather than trusted;
  - SQLite's own waiters are INVISIBLE there: it takes locks with the
    non-blocking ``F_SETLK`` and waits by sleeping and retrying (busy_timeout),
    so a connection waiting on SQLite never appears as a ``->`` (blocked
    waiter) line. MEASURED: 0 such lines while a peer busy-waited on the write
    lock. The parser still keeps ``->`` lines (``waiting: true``) for other
    lock users on the same files.

POSIX locks belong to a PROCESS, so a holder inside this process shows in the
lock table only as this pid (labelled ``self``). To name it, the database
factories register every connection they open, with the code that called the
factory (``site``, ``module:function``), and the snapshot lists each one on the
same database: its kind, site, ``in_transaction`` (a write transaction holds
the write lock until commit, whether or not anything is running), and for a
``SerializedConnection`` whether its asyncio lock is held and the statement
holding it. Registered: ``get_db`` (and ``init_db``, through it) and any
``SerializedConnection``, ``get_raw_db``, ``connect_aiosqlite_rw``, and
``connect_sqlite_rw`` (through a ``sqlite3.Connection`` subclass passed as
``factory=``, unless the caller passes its own factory). NOT registered, so
invisible here: direct ``sqlite3.connect`` / ``aiosqlite.connect`` calls
outside those factories (attention, eval, browser profile, payments vault,
guardian watchdog, migrations, integrity and others), and the read-only pool
(``open_ro_connection``), which cannot hold the write lock. The snapshot also
counts this event loop's tasks by name. Other pids are labelled with
``pid_breakdown.process_label`` (argv[0], plus a module or script name; never
another argument, which can carry credentials).

The snapshot preserves the SQLite error name (``SQLITE_BUSY``,
``SQLITE_BUSY_SNAPSHOT``, ...), separately from ``lock_table_error`` when the
kernel table is unreadable. BUSY_SNAPSHOT means a stale read transaction;
retrying it cannot refresh its snapshot, regardless of any concurrent writer.
``writer_pids`` lists positive-PID, nonwaiting POSIX WRITE locks whose shm
range includes byte 120; raw rows retain other lock types, readers and waiters.

Statement text is ``_slow_log.sql_label`` (whitespace collapsed, 32-hex names
collapsed, cut at 120 characters, the full length reported as ``*_chars``);
bound parameters never reach it, but literals written inline in the SQL do
(within its first 120 characters).

Snapshots go to ``<genesis_home>/logs/lock_snapshots/``; the writer prunes
files older than 7 days and keeps the directory under 20 MB (the same rule as
``pid_snapshots``). One capture per database per process per 60 s at most.
``GENESIS_LOCK_SNAPSHOT_DISABLED=1`` turns it off.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import weakref
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DISABLE_ENV = "GENESIS_LOCK_SNAPSHOT_DISABLED"
# Module-level so tests can repoint it at a fake table.
_PROC_LOCKS = "/proc/locks"
# The lock table holds every lock on the box; stop reading past this many bytes
# (a pathological table must not turn a diagnostic into a long stall).
_PROC_LOCKS_BYTES = 4 * 1024 * 1024
_INTERVAL_S = 60.0
# asyncio's default task names; folded into one count so they cannot crowd out
# the named tasks.
_DEFAULT_TASK_NAME = re.compile(r"^Task-\d+$")
# Frames skipped when naming the code that opened a connection: the factories
# themselves and the context-manager / event-loop machinery between them and it.
_SITE_SKIP = ("genesis.db.connection", "genesis.db.lock_holders", "contextlib", "asyncio")

_clock = time.monotonic  # test seam
# Database path -> when its last capture was scheduled.
_last_capture_at: dict[str, float] = {}
# Strong references: the event loop keeps only weak ones to its tasks.
_pending: set[asyncio.Task] = set()
# Every live connection a factory opened in this process -> what it is. Weak, so
# registration never keeps a connection alive. Guarded: factories run on worker
# threads too, and a WeakKeyDictionary cannot be copied while another thread
# adds to it.
_connections: weakref.WeakKeyDictionary[Any, dict] = weakref.WeakKeyDictionary()
_connections_lock = threading.Lock()

_SHM_NAMES = {120: "writer", 121: "checkpointer", 122: "recoverer", 128: "open (DMS)"}
_SHM_READ_MARK_FIRST = 123
_SHM_READ_MARK_LAST = 127


def _reset_rate_limit() -> None:
    """Forget the last capture times (tests only)."""
    _last_capture_at.clear()


def caller_site() -> str:
    """``module:function`` of the nearest frame outside the DB factories."""
    try:
        frame = sys._getframe(1)
        while frame is not None:
            module = frame.f_globals.get("__name__", "?")
            if not module.startswith(_SITE_SKIP):
                return f"{module}:{frame.f_code.co_name}"
            frame = frame.f_back
    except Exception:
        pass
    return "?"


def register(conn: Any, *, db_path: str | Path, kind: str, site: str | None = None) -> None:
    """Track a connection a factory opened, so a snapshot can name it.

    ``kind`` is ``serialized``, ``aiosqlite`` or ``sqlite3``. Never raises: a
    failed registration costs only this diagnostic."""
    try:
        entry = {
            "kind": kind,
            "site": site if site is not None else caller_site(),
            # Resolved once here (the open is doing I/O anyway), so the
            # capture compares strings and touches no filesystem.
            "db_path": os.path.realpath(db_path),
        }
        with _connections_lock:
            _connections[conn] = entry
    except Exception:
        logger.debug("lock-holder registration failed", exc_info=True)


class TrackedSqliteConnection(sqlite3.Connection):
    """``sqlite3.Connection`` that a weak registry can hold (the base type has
    no ``__weakref__`` slot). ``connect_sqlite_rw`` passes it as ``factory=``."""


def parse_proc_locks(text: str) -> list[dict]:
    """Parse ``/proc/locks`` lines; a line that does not parse is skipped.

    Format: ``N: [-> ]TYPE MODE ACCESS PID MAJ:MIN:INODE START END``, where
    ``->`` marks a waiter and END may be ``EOF``."""
    rows: list[dict] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        fields = fields[1:]  # the "N:" ordinal
        waiting = fields[0] == "->"
        if waiting:
            fields = fields[1:]
        if len(fields) < 7:
            continue
        kind, _mode, access, pid, ident, start, end = fields[:7]
        dev, sep, inode = ident.rpartition(":")
        if not sep:
            continue
        try:
            row = {
                "pid": int(pid),
                "type": kind,
                "access": access,
                "dev": dev,
                "inode": int(inode),
                "start": int(start),
                "end": None if end == "EOF" else int(end),
                "waiting": waiting,
            }
        except ValueError:
            continue
        rows.append(row)
    return rows


def decode(file: str, start: int, end: int | None) -> str:
    """Name a locked byte range of one of the database's files."""
    if file == "shm":
        if start == end:
            if start in _SHM_NAMES:
                return _SHM_NAMES[start]
            if _SHM_READ_MARK_FIRST <= start <= _SHM_READ_MARK_LAST:
                return f"reader-mark {start - _SHM_READ_MARK_FIRST}"
        return f"shm bytes {start}-{'EOF' if end is None else end}"
    if file == "main":
        return "main-db"
    return file


def _db_inodes(db_path: Path) -> dict[int, tuple[str, int]]:
    """``{inode: (file role, st_dev)}`` for the database and its WAL files that exist."""
    found: dict[int, tuple[str, int]] = {}
    for role, path in (
        ("main", str(db_path)),
        ("wal", f"{db_path}-wal"),
        ("shm", f"{db_path}-shm"),
    ):
        try:
            st = os.stat(path)
        except OSError:
            continue
        found[st.st_ino] = (role, st.st_dev)
    return found


def _dev_text(st_dev: int) -> str:
    return f"{os.major(st_dev):02x}:{os.minor(st_dev):02x}"


def _default_label(pid: int) -> str:
    from genesis.observability.pid_breakdown import process_label

    return process_label(pid)


def holders_for(
    db_path: Path,
    locks_text: str,
    *,
    label: Callable[[int], str] | None = None,
) -> dict:
    """The lock-table lines on ``db_path``'s files, named and labelled."""
    label = label or _default_label
    inodes = _db_inodes(db_path)
    me = os.getpid()
    labels: dict[int, str] = {}
    holders: list[dict] = []
    device_mismatch = False
    for row in parse_proc_locks(locks_text):
        hit = inodes.get(row["inode"])
        if hit is None:
            continue
        role, st_dev = hit
        if row["dev"] != _dev_text(st_dev):
            device_mismatch = True
        pid = row["pid"]
        if pid not in labels:
            try:
                labels[pid] = "self" if pid == me else label(pid)
            except Exception:
                labels[pid] = "?"
        holders.append(
            {
                "pid": pid,
                "label": labels[pid],
                "self": pid == me,
                "file": role,
                "type": row["type"],
                "access": row["access"],
                "lock": decode(role, row["start"], row["end"]),
                "range": [row["start"], row["end"]],
                # As the kernel printed it; not used for matching (it need
                # not equal st_dev, e.g. inside a container).
                "dev": row["dev"],
                "waiting": row["waiting"],
            }
        )
    writer_pids = sorted({
        h["pid"] for h in holders
        if h["file"] == "shm"
        and h["type"] == "POSIX"
        and h["access"] == "WRITE"
        and h["pid"] > 0
        and not h["waiting"]
        and h["range"][0] <= 120
        and (h["range"][1] is None or h["range"][1] >= 120)
    })
    return {
        "holders": holders,
        "writer_pids": writer_pids,
        "device_mismatch": device_mismatch,
        "files_found": sorted(role for role, _dev in inodes.values()),
    }


def capture(db_path: Path) -> dict:
    """Read the lock table and report ``db_path``'s holders. Blocking; never
    raises for an unreadable table (``readable: false`` and ``lock_table_error``)."""
    db_path = Path(db_path)
    snapshot: dict = {
        "taken_at": datetime.now(UTC).isoformat(),
        "db_path": str(db_path),
        "pid": os.getpid(),
    }
    try:
        with open(_PROC_LOCKS, encoding="utf-8", errors="replace") as f:
            text = f.read(_PROC_LOCKS_BYTES + 1)
    except OSError as exc:
        snapshot.update(
            readable=False,
            lock_table_error=f"{type(exc).__name__}: {exc}",
            holders=[],
            writer_pids=[],
        )
        return snapshot
    snapshot["readable"] = True
    snapshot["lock_table_truncated"] = len(text) > _PROC_LOCKS_BYTES
    snapshot.update(holders_for(db_path, text[:_PROC_LOCKS_BYTES]))
    return snapshot


def _snapshot_dir() -> Path:
    from genesis.env import genesis_home

    return genesis_home() / "logs" / "lock_snapshots"


def snapshot_path() -> Path:
    """Where the next snapshot will be written (named before it is taken, so
    the warning line can carry it)."""
    name = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + f"-{os.getpid()}.json"
    return _snapshot_dir() / name


def capture_and_write(target: Path, db_path: Path, context: dict) -> None:
    """Capture, write ``target`` atomically, prune the directory. Blocking."""
    # Imported here, not at module top: genesis.observability's package init
    # pulls in aiohttp (~250 ms), and genesis.db.connection imports this
    # module, so a top-level import taxed every hook that opens the DB.
    from genesis.observability.pid_breakdown import prune_snapshot_dir
    from genesis.util.atomic import atomic_write_text

    snapshot = {**context, **capture(db_path)}
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, json.dumps(snapshot, indent=1), encoding="utf-8")
    # Same retention rule as pid_snapshots (7 days, 20 MB), applied to this directory.
    prune_snapshot_dir(target.parent, time.time())


def _capture_quietly(target: Path, db_path: Path, context: dict) -> None:
    try:
        capture_and_write(target, db_path, context)
    except Exception:
        logger.warning("DB lock-holder snapshot failed: %s", target, exc_info=True)


def _statement(sql: object) -> tuple[str | None, int | None]:
    if sql is None:
        return None, None
    from genesis.db._slow_log import sql_label

    return sql_label(sql), len(" ".join(str(sql).split()))


def _in_transaction(conn: Any, kind: str) -> bool | None:
    """Read across threads (MEASURED to work for sqlite3 and aiosqlite)."""
    try:
        raw = conn._conn if kind == "serialized" else conn
        return bool(raw.in_transaction)
    except Exception:
        return None  # closed, or not readable from this thread


def _in_process_connections(this: Any) -> list[dict]:
    """Every registered connection on ``this`` connection's database.

    Attribute reads only, on the loop thread: no awaits, no I/O."""
    with _connections_lock:
        entries = list(_connections.items())
        target = _connections.get(this, {}).get("db_path")
    if target is None:
        return []
    rows: list[dict] = []
    for conn, entry in entries:
        if entry["db_path"] != target:
            continue
        kind = entry["kind"]
        row = {
            "kind": kind,
            "site": entry["site"],
            "db_path": entry["db_path"],
            "this_connection": conn is this,
            "in_transaction": _in_transaction(conn, kind),
        }
        if kind == "serialized":
            try:
                text, chars = _statement(conn._holder)
                row["lock_held"] = conn._lock.locked()
                row["statement"] = text
                row["statement_chars"] = chars
            except Exception:
                row["error"] = "unreadable connection state"
        rows.append(row)
    return rows


def _task_counts() -> tuple[dict[str, int], int]:
    """Task names with counts; asyncio's default names fold into ``Task-*``."""
    names = [t.get_name() for t in asyncio.all_tasks()]
    counts = Counter(_DEFAULT_TASK_NAME.sub("Task-*", n) for n in names)
    return dict(sorted(counts.items())), len(names)


def schedule_capture(
    conn: Any,
    *,
    error: str | None,
    attempts: int,
    backoff_s: float,
) -> tuple[Path | None, str]:
    """Schedule one lock-holder snapshot for ``conn`` (a SerializedConnection).

    Returns ``(file it will be written to, text for the log line)``; the file
    is None, and the text names why, when disabled, rate-limited (one capture
    per database per process per 60 s), there is no database path, or
    scheduling failed.

    Called while ``conn``'s lock is held, so it does no I/O and awaits
    nothing: the in-process state is read synchronously here, and the lock
    table is read on a worker thread through a tracked task. Never raises."""
    try:
        db_path = conn._db_path
        if db_path is None:
            return None, "not captured (no database path)"
        if os.environ.get(DISABLE_ENV) == "1":
            return None, "not captured (disabled)"
        # Registration already resolved the open connection's identity.
        # Reuse it for both the rate window and worker without filesystem I/O
        # here; the original connection/admission path remains untouched.
        with _connections_lock:
            db_path = _connections.get(conn, {}).get("db_path")
        if not isinstance(db_path, str) or not db_path:
            return None, "not captured (database identity unavailable)"
        key = db_path
        now = _clock()
        last = _last_capture_at.get(key)
        if last is not None and now - last < _INTERVAL_S:
            return None, "not captured (rate-limited)"
        target = snapshot_path()
        text, chars = _statement(conn._holder)
        tasks, tasks_total = _task_counts()
        context = {
            "attempts": attempts,
            "backoff_s": round(backoff_s, 3),
            # SQLITE_BUSY: a holder outlived busy_timeout and the retries.
            # SQLITE_BUSY_SNAPSHOT: this read snapshot is stale; current
            # holders need not have caused the failure.
            "error": error,
            # The statement that gave up (this connection's own).
            "statement": text,
            "statement_chars": chars,
            "in_transaction": _in_transaction(conn, "serialized"),
            # This process's connections on the same database (the lock table
            # shows them all as one pid).
            "in_process_connections": _in_process_connections(conn),
            # {name: count}; asyncio's unnamed "Task-N" fold into "Task-*".
            "tasks": tasks,
            "tasks_total": tasks_total,
        }
        from genesis.util.tasks import tracked_task

        coro = asyncio.to_thread(_capture_quietly, target, Path(db_path), context)
        try:
            task = tracked_task(coro, name="db-lock-holder-snapshot")
        except BaseException:
            coro.close()
            raise
        # Only a scheduled capture uses up the window.
        _last_capture_at[key] = now
        _pending.add(task)
        task.add_done_callback(_pending.discard)
        return target, str(target)
    except Exception:
        logger.warning("DB lock-holder snapshot failed to schedule", exc_info=True)
        return None, "not captured (schedule failed)"
