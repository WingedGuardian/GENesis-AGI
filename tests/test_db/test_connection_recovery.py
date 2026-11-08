"""Tests for SerializedConnection lock error tracking and recovery (A3).

WS-1 PR-1 semantics: every SQL call retries lock errors in-place (bounded,
jittered) before surfacing them, so the consecutive-error counter counts
exhausted retry EPISODES — one per failed call — not raw underlying attempts.
Reconnect-after-N still works, at episode granularity.
"""

import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from genesis.db import connection as connection_mod
from genesis.db.connection import _WRITE_RETRY_DELAYS, SerializedConnection

# One episode = the initial attempt + one retry per configured delay.
ATTEMPTS_PER_EPISODE = len(_WRITE_RETRY_DELAYS) + 1


@pytest.fixture(autouse=True)
def instant_retry_sleep(monkeypatch):
    """Make retry backoff instant — these tests assert counting/reconnect
    semantics, not the schedule (test_write_retry.py covers delays/jitter)."""

    async def _instant(_delay: float) -> None:
        return None

    monkeypatch.setattr(connection_mod, "_async_sleep", _instant)


@pytest.fixture
async def recovery_conn(tmp_path):
    """SerializedConnection with reconnect_fn for testing."""
    db_path = tmp_path / "test.db"
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("CREATE TABLE t(x)")
    await conn.commit()

    async def _reconnect():
        c = await aiosqlite.connect(str(db_path))
        c.row_factory = aiosqlite.Row
        await c.execute("PRAGMA journal_mode=WAL")
        return c

    sc = SerializedConnection(conn, reconnect_fn=_reconnect)
    yield sc
    import contextlib

    with contextlib.suppress(Exception):
        await sc.close()


async def test_error_counter_resets_on_success(recovery_conn):
    """Successful operations reset the consecutive error counter."""
    await recovery_conn.execute("INSERT INTO t VALUES (1)")
    await recovery_conn.commit()
    assert recovery_conn._consecutive_errors == 0


async def test_transient_lock_recovers_within_episode(recovery_conn):
    """A lock that clears within the retry budget never surfaces to the caller
    and leaves the counter at 0 — the convoy-loss case PR-1 exists for."""
    original = recovery_conn._conn.execute

    call_count = 0

    async def flaky_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise sqlite3.OperationalError("database is locked")
        return await original(sql, params)

    recovery_conn._conn.execute = flaky_execute

    cur = await recovery_conn.execute("SELECT 1")
    assert await cur.fetchone() is not None
    assert call_count == 3  # 2 failures + the succeeding retry
    assert recovery_conn._consecutive_errors == 0


async def test_exhausted_episode_counts_once_and_reraises(recovery_conn):
    """A lock that outlives every retry re-raises and counts ONE episode —
    the counter tracks episodes now, not raw attempts."""
    attempts = 0

    async def always_fail(sql, params=None):
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("database is locked")

    recovery_conn._conn.execute = always_fail

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        await recovery_conn.execute("SELECT 1")
    assert attempts == ATTEMPTS_PER_EPISODE
    assert recovery_conn._consecutive_errors == 1

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        await recovery_conn.execute("SELECT 1")
    assert recovery_conn._consecutive_errors == 2


async def test_reconnect_after_threshold(recovery_conn):
    """After _max_errors consecutive exhausted EPISODES, reconnection fires."""
    recovery_conn._max_errors = 3  # Lower threshold for testing
    original_conn = recovery_conn._conn

    async def always_fail(sql, params=None):
        raise sqlite3.OperationalError("database is locked")

    recovery_conn._conn.execute = always_fail

    # First 2 exhausted episodes — just increment counter
    for _ in range(2):
        with pytest.raises(sqlite3.OperationalError):
            await recovery_conn.execute("SELECT 1")

    assert recovery_conn._consecutive_errors == 2

    # 3rd episode — triggers reconnect
    with pytest.raises(sqlite3.OperationalError):
        await recovery_conn.execute("SELECT 1")

    # After reconnect, counter resets and conn is new
    assert recovery_conn._consecutive_errors == 0
    assert recovery_conn._conn is not original_conn


async def test_no_reconnect_without_fn():
    """Without reconnect_fn, exhausted episodes just increment and re-raise."""
    conn = await aiosqlite.connect(":memory:")
    sc = SerializedConnection(conn)  # No reconnect_fn

    async def fake_fail(sql, params=None):
        raise sqlite3.OperationalError("database is locked")

    conn.execute = fake_fail

    for _ in range(10):
        with pytest.raises(sqlite3.OperationalError):
            await sc.execute("SELECT 1")

    # Counter goes up (one per episode) but no crash
    assert sc._consecutive_errors == 10
    await sc.close()


# -- Lock-holder snapshot on exhaustion (issue #3528) -------------------------


class _Locked:
    """What the ``locked_conn`` fixture hands a test."""

    def __init__(self, sc, calls, now, real_writer):
        self.sc = sc
        self.calls = calls
        self.now = now
        self.real_writer = real_writer

    async def settle(self) -> None:
        import asyncio

        from genesis.db import lock_holders

        if lock_holders._pending:
            await asyncio.gather(*list(lock_holders._pending))


@pytest.fixture
async def locked_conn(tmp_path, monkeypatch):
    """A SerializedConnection with a db_path whose every execute is locked,
    capture enabled, snapshots under a tmp GENESIS_HOME, a controllable clock,
    and the capture replaced by a recorder (the real one is covered in
    test_lock_holders.py; ``real_writer`` restores it)."""
    from genesis.db import lock_holders

    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(lock_holders.DISABLE_ENV, raising=False)
    lock_holders._reset_rate_limit()
    now = [1000.0]
    monkeypatch.setattr(lock_holders, "_clock", lambda: now[0])
    calls: list[tuple] = []
    real_writer = lock_holders.capture_and_write

    def _record(target, db_path, context):
        calls.append((target, db_path, context))

    monkeypatch.setattr(lock_holders, "capture_and_write", _record)

    db_path = tmp_path / "locked.db"
    conn = await aiosqlite.connect(str(db_path))

    def _busy() -> sqlite3.OperationalError:
        # What the sqlite3 module raises for SQLITE_BUSY (it sets the name).
        err = sqlite3.OperationalError("database is locked")
        err.sqlite_errorcode = 5
        err.sqlite_errorname = "SQLITE_BUSY"
        return err

    async def fake_fail(sql, params=None):
        raise _busy()

    async def fake_script(sql):
        raise _busy()

    conn.execute = fake_fail
    conn.executescript = fake_script
    locked = _Locked(SerializedConnection(conn, db_path=db_path), calls, now, real_writer)
    yield locked
    await locked.settle()
    lock_holders._reset_rate_limit()
    await locked.sc.close()


async def test_exhaustion_schedules_one_capture_per_window(locked_conn, caplog):
    lc = locked_conn
    with caplog.at_level("WARNING", logger="genesis.db.connection"):
        for _ in range(3):
            with pytest.raises(sqlite3.OperationalError):
                await lc.sc.execute("UPDATE t SET x = ?", ("secret-value",))
    await lc.settle()
    assert len(lc.calls) == 1
    target, db_path, context = lc.calls[0]
    assert db_path == lc.sc._db_path
    assert context["statement"] == "UPDATE t SET x = ?"
    assert context["attempts"] == ATTEMPTS_PER_EPISODE
    assert "secret-value" not in repr(context)  # never a bound parameter value
    giving_up = [r.getMessage() for r in caplog.records if "giving up" in r.getMessage()]
    assert len(giving_up) == 3
    assert str(target) in giving_up[0]
    assert str(target) not in giving_up[1]  # rate-limited: no capture, no path
    assert "not captured (rate-limited)" in giving_up[1]
    assert "cancelled_statement" not in context  # always null at exhaustion: dropped
    # The SQLite error kind and this connection's transaction state are recorded:
    # SQLITE_BUSY_SNAPSHOT rejects a stale read snapshot; retrying it is insufficient.
    assert context["error"] == "SQLITE_BUSY"
    assert context["in_transaction"] is False

    lc.now[0] += 61.0
    with pytest.raises(sqlite3.OperationalError):
        await lc.sc.execute("SELECT 1")
    await lc.settle()
    assert len(lc.calls) == 2


async def test_exhaustion_survives_a_failing_capture(locked_conn, monkeypatch, caplog):
    from genesis.db import lock_holders

    lc = locked_conn

    def _boom(target, db_path, context):
        raise RuntimeError("capture exploded")

    monkeypatch.setattr(lock_holders, "capture_and_write", _boom)
    with caplog.at_level("WARNING"):
        with pytest.raises(sqlite3.OperationalError):
            await lc.sc.execute("SELECT 1")
        await lc.settle()
    assert any("lock-holder snapshot failed" in r.getMessage() for r in caplog.records)


async def test_snapshot_rate_limit_uses_the_registered_database_identity(locked_conn, monkeypatch):
    from genesis.db import lock_holders

    lc = locked_conn
    original = lc.sc._db_path
    alias = original.with_name("alias.db")
    alias.symlink_to(original)
    monkeypatch.chdir(original.parent)
    spellings = [original, alias, Path(original.name)]
    connections = [lc.sc, *[
        SerializedConnection(lc.sc._conn, db_path=path) for path in spellings[1:]
    ]]
    results = [
        lock_holders.schedule_capture(conn, error="SQLITE_BUSY", attempts=1, backoff_s=0)
        for conn in connections
    ]
    await lc.settle()
    assert len(lc.calls) == 1
    assert results[0][0] is not None
    assert all(target is None and "rate-limited" in text for target, text in results[1:])


async def test_missing_registered_identity_skips_without_spending_a_window(locked_conn):
    from genesis.db import lock_holders

    lc = locked_conn
    with lock_holders._connections_lock:
        entry = lock_holders._connections.pop(lc.sc)
    target, text = lock_holders.schedule_capture(
        lc.sc, error="SQLITE_BUSY", attempts=1, backoff_s=0,
    )
    await lc.settle()
    assert target is None and "identity unavailable" in text
    assert not lc.calls
    with lock_holders._connections_lock:
        lock_holders._connections[lc.sc] = entry
    target, _ = lock_holders.schedule_capture(
        lc.sc, error="SQLITE_BUSY", attempts=1, backoff_s=0,
    )
    await lc.settle()
    assert target is not None and len(lc.calls) == 1


async def test_exhaustion_survives_a_failing_schedule(locked_conn, monkeypatch, caplog):
    from genesis.db import lock_holders

    lc = locked_conn
    real_path = lock_holders.snapshot_path
    failures = [1]

    def _fails_once():
        if failures:
            failures.pop()
            raise RuntimeError("no snapshot dir")
        return real_path()

    monkeypatch.setattr(lock_holders, "snapshot_path", _fails_once)
    with caplog.at_level("WARNING"), pytest.raises(sqlite3.OperationalError):
        await lc.sc.execute("SELECT 1")
    assert lc.calls == []
    assert any("lock-holder snapshot failed" in r.getMessage() for r in caplog.records)
    giving_up = [r.getMessage() for r in caplog.records if "giving up" in r.getMessage()]
    assert giving_up and "not captured (schedule failed)" in giving_up[0]

    # A failed schedule does not use up the 60 s window: the next one captures.
    with pytest.raises(sqlite3.OperationalError):
        await lc.sc.execute("SELECT 1")
    await lc.settle()
    assert len(lc.calls) == 1


async def test_rate_limit_is_per_database(locked_conn, tmp_path):
    """A storm on one database must not hide a lock on another."""
    lc = locked_conn
    other_path = tmp_path / "other.db"
    other_raw = await aiosqlite.connect(str(other_path))
    other_raw.execute = lc.sc._conn.execute
    other = SerializedConnection(other_raw, db_path=other_path)
    try:
        for conn in (lc.sc, lc.sc, other):
            with pytest.raises(sqlite3.OperationalError):
                await conn.execute("SELECT 1")
        await lc.settle()
        assert [c[1] for c in lc.calls] == [lc.sc._db_path, other_path]
    finally:
        await other.close()


def _same_db(context: dict) -> list[dict]:
    return context["in_process_connections"]


async def test_in_process_writer_with_released_lock_is_named(locked_conn):
    """Legacy implicit transactions: a writer that ran INSERT holds SQLite's
    write lock until commit() while its asyncio lock is already released. POSIX
    locks are per process, so the lock table shows it only as 'self'; the
    snapshot must name it from this process's own connections."""
    from genesis.db.connection import get_db

    lc = locked_conn
    writer = await get_db(lc.sc._db_path)
    try:
        await writer.execute("CREATE TABLE IF NOT EXISTS w(x)")
        await writer.commit()
        await writer.execute("INSERT INTO w VALUES (?)", ("private",))
        assert not writer._lock.locked() and writer.in_transaction
        with pytest.raises(sqlite3.OperationalError):
            await lc.sc.execute("DELETE FROM t WHERE x = ?", ("private",))
        await lc.settle()
    finally:
        await writer.rollback()
        await writer.close()
    _target, _db, context = lc.calls[0]
    rows = _same_db(context)
    mine = f"{__name__}:test_in_process_writer_with_released_lock_is_named"
    named = [r for r in rows if r["site"] == mine]
    assert len(named) == 1, rows
    w = named[0]
    assert w["kind"] == "serialized"
    assert w["lock_held"] is False and w["in_transaction"] is True
    assert w["statement"] == "INSERT INTO w VALUES (?)"
    assert w["this_connection"] is False
    this = [r for r in rows if r["this_connection"]]
    assert len(this) == 1 and this[0]["lock_held"] is True
    assert "private" not in repr(context)


async def test_every_factory_registers_its_connections_with_the_caller_site(locked_conn, tmp_path):
    """get_db, get_raw_db, connect_aiosqlite_rw and connect_sqlite_rw each
    register what they open, named by the code that called the factory (not
    by the factory's own frames), and only connections on the same database
    are reported."""
    from genesis.db.connection import (
        connect_aiosqlite_rw,
        connect_sqlite_rw,
        get_db,
        get_raw_db,
    )

    lc = locked_conn
    db = lc.sc._db_path
    here = f"{__name__}:test_every_factory_registers_its_connections_with_the_caller_site"
    serialized = await get_db(db)
    other = await get_db(tmp_path / "elsewhere.db")
    sync_conn = connect_sqlite_rw(db, timeout=1.0)
    try:
        async with get_raw_db(db) as raw, connect_aiosqlite_rw(db) as guarded:
            await raw.execute("CREATE TABLE IF NOT EXISTS w(x)")
            await raw.execute("INSERT INTO w VALUES (1)")  # raw holds the write lock
            assert raw.in_transaction and guarded is not None
            with pytest.raises(sqlite3.OperationalError):
                await lc.sc.execute("SELECT 1")
            await lc.settle()
            await raw.rollback()
    finally:
        sync_conn.close()
        await serialized.close()
        await other.close()
    _target, _db, context = lc.calls[0]
    rows = [r for r in _same_db(context) if r["site"] == here]
    kinds = sorted(r["kind"] for r in rows)
    assert kinds == ["aiosqlite", "aiosqlite", "serialized", "sqlite3"], rows
    assert all(r["db_path"] == str(db.resolve()) for r in rows)
    writers = [r for r in rows if r["in_transaction"]]
    assert len(writers) == 1 and writers[0]["kind"] == "aiosqlite"
    assert "lock_held" not in writers[0]  # a raw connection has no asyncio lock
    assert not any("elsewhere" in str(r["db_path"]) for r in _same_db(context))


async def test_task_names_are_counted_not_truncated(locked_conn):
    """Unnamed tasks ("Task-N") are folded into one count, so a named task is
    never pushed out by hundreds of anonymous ones."""
    import asyncio

    lc = locked_conn
    release = asyncio.Event()
    anonymous = [asyncio.create_task(release.wait()) for _ in range(250)]
    named = asyncio.create_task(release.wait(), name="zz-named-task")
    try:
        with pytest.raises(sqlite3.OperationalError):
            await lc.sc.execute("SELECT 1")
        await lc.settle()
    finally:
        release.set()
        await asyncio.gather(*anonymous, named)
    _target, _db, context = lc.calls[0]
    assert context["tasks"]["zz-named-task"] == 1
    assert context["tasks"]["Task-*"] >= 250
    assert context["tasks_total"] == sum(context["tasks"].values())


async def test_close_and_cursor_name_themselves_as_holder(recovery_conn):
    await recovery_conn.cursor()
    assert recovery_conn._holder == "<cursor>"
    await recovery_conn.close()
    assert recovery_conn._holder == "<close>"


async def test_executescript_lock_error_captures(locked_conn, caplog):
    lc = locked_conn
    with (
        caplog.at_level("WARNING", logger="genesis.db.connection"),
        pytest.raises(sqlite3.OperationalError),
    ):
        await lc.sc.executescript("CREATE TABLE z(a); INSERT INTO z VALUES (1);")
    await lc.settle()
    assert len(lc.calls) == 1
    target, _db, context = lc.calls[0]
    assert context["attempts"] == 1 and context["error"] == "SQLITE_BUSY"
    assert any(str(target) in r.getMessage() for r in caplog.records)


async def test_exhaustion_with_unreadable_lock_table_still_writes(
    locked_conn, monkeypatch, tmp_path
):
    import json

    from genesis.db import lock_holders

    lc = locked_conn
    monkeypatch.setattr(lock_holders, "capture_and_write", lc.real_writer)
    monkeypatch.setattr(lock_holders, "_PROC_LOCKS", str(tmp_path / "no-such-locks"))
    with pytest.raises(sqlite3.OperationalError):
        await lc.sc.execute("SELECT 1")
    await lc.settle()
    files = list((tmp_path / "home" / "logs" / "lock_snapshots").glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["readable"] is False
    assert data["statement"] == "SELECT 1"
    assert data["error"] == "SQLITE_BUSY"
    assert "no-such-locks" in data["lock_table_error"]


async def test_disabled_env_skips_capture(locked_conn, monkeypatch, caplog):
    from genesis.db import lock_holders

    lc = locked_conn
    monkeypatch.setenv(lock_holders.DISABLE_ENV, "1")
    with (
        caplog.at_level("WARNING", logger="genesis.db.connection"),
        pytest.raises(sqlite3.OperationalError),
    ):
        await lc.sc.execute("SELECT 1")
    await lc.settle()
    assert lc.calls == []
    assert any(
        "giving up" in r.getMessage() and "not captured (disabled)" in r.getMessage()
        for r in caplog.records
    )
    assert not lock_holders._pending
