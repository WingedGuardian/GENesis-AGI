"""Real scratch SQLite receipts for lock-spanning single-statement writes."""

import asyncio
import sqlite3
import threading
from contextlib import suppress
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db import connection as connection_mod
from genesis.db.connection import PendingTransactionError, SerializedConnection


@pytest.fixture
async def database(tmp_path):
    path = tmp_path / "committed.sqlite"

    async def reopen():
        return await aiosqlite.connect(path)

    raw = await reopen()
    await raw.execute("CREATE TABLE sample(value INTEGER)")
    await raw.commit()
    conn = SerializedConnection(raw, reconnect_fn=reopen)
    yield conn, path
    with suppress(Exception):
        await conn.close()


async def observed(path):
    async with aiosqlite.connect(path) as db, db.execute("SELECT value FROM sample ORDER BY value") as cursor:
        return [row[0] for row in await cursor.fetchall()]


@pytest.mark.parametrize("sql,parameters", [
    ("INSERT INTO sample VALUES(?)", (1,)),
    ("INSERT INTO sample VALUES(:value)", {"value": 1}),
    ("INSERT INTO sample VALUES(?)", iter([1])),
])
async def test_success_is_visible_to_independent_connection(database, sql, parameters):
    conn, path = database
    assert await conn.execute_committed(sql, parameters) == 1
    assert await observed(path) == [1]
    assert not conn.in_transaction
    assert await conn.execute_committed("UPDATE sample SET value=2 WHERE value=9") == 0


async def test_foreign_pending_work_is_refused_untouched_then_retry_succeeds(database):
    conn, path = database
    await conn.execute("INSERT INTO sample VALUES(7)")
    with pytest.raises(PendingTransactionError):
        await conn.execute_committed("INSERT INTO sample VALUES(8)")
    assert conn.in_transaction
    assert await observed(path) == []
    await conn.commit()
    assert await observed(path) == [7]
    assert await conn.execute_committed("INSERT INTO sample VALUES(8)") == 1
    assert await observed(path) == [7, 8]


@pytest.mark.parametrize("remove_lock", [False, True])
async def test_concurrent_rollback_control_requires_spanning_lock(database, remove_lock):
    conn, path = database
    entered, release = asyncio.Event(), asyncio.Event()
    original = conn._conn.commit

    async def delayed_commit():
        entered.set()
        await release.wait()
        await original()

    conn._conn.commit = delayed_commit
    if remove_lock:
        class NoLock:
            async def __aenter__(self):
                pass

            async def __aexit__(self, *args):
                pass

        conn._lock = NoLock()
    writer = asyncio.create_task(conn.execute_committed("INSERT INTO sample VALUES(1)"))
    await entered.wait()
    rollback = asyncio.create_task(conn.rollback())
    try:
        if remove_lock:
            await rollback
        else:
            await asyncio.sleep(0)
            assert not rollback.done()
    finally:
        release.set()
    assert await writer == 1
    await rollback
    assert await observed(path) == ([] if remove_lock else [1])


@pytest.mark.parametrize("failure", ["execute", "cursor_close", "commit"])
async def test_operation_failure_cleans_own_transaction(database, failure):
    conn, path = database
    sql = "INSERT INTO missing VALUES(1)" if failure == "execute" else "INSERT INTO sample VALUES(1)"
    if failure == "commit":
        conn._conn.commit = AsyncMock(side_effect=RuntimeError("fixture commit"))
    if failure == "cursor_close":
        original = conn._conn.execute

        async def bad_cursor(*args):
            cursor = await original(*args)
            cursor.close = AsyncMock(side_effect=RuntimeError("fixture cursor"))
            return cursor

        conn._conn.execute = bad_cursor
    with pytest.raises((sqlite3.OperationalError, RuntimeError)):
        await conn.execute_committed(sql)
    assert not conn.in_transaction
    assert await observed(path) == []


async def test_repeated_cancellation_cannot_release_lock_before_cleanup(database):
    conn, path = database
    committing, rolling_back, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = conn._conn.rollback

    async def commit_gate():
        committing.set()
        await asyncio.Event().wait()

    async def rollback_gate():
        rolling_back.set()
        await release.wait()
        await original()

    conn._conn.commit = commit_gate
    conn._conn.rollback = rollback_gate
    writer = asyncio.create_task(conn.execute_committed("INSERT INTO sample VALUES(1)"))
    await committing.wait()
    writer.cancel()
    await rolling_back.wait()
    reader = asyncio.create_task(conn.execute_fetchall("SELECT value FROM sample"))
    try:
        for _ in range(3):
            writer.cancel()
            await asyncio.sleep(0)
            assert conn._lock.locked() and not writer.done() and not reader.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await writer
    assert await reader == []
    assert not conn.in_transaction and await observed(path) == []


async def test_cancelled_queued_commit_can_persist_but_never_returns_success(database):
    conn, path = database
    entered, release = asyncio.Event(), threading.Event()
    original = conn._conn.commit
    blocker = None
    loop = asyncio.get_running_loop()

    def worker_gate():
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(60):
            raise TimeoutError("fixture worker gate")

    async def queued_commit():
        nonlocal blocker
        blocker = asyncio.create_task(conn._conn._execute(worker_gate))
        await entered.wait()
        await original()

    conn._conn.commit = queued_commit
    writer = asyncio.create_task(conn.execute_committed("INSERT INTO sample VALUES(1)"))
    try:
        await entered.wait()
        await asyncio.sleep(0)
        writer.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await writer
    assert blocker is not None
    await blocker
    assert not conn.in_transaction
    assert await observed(path) == [1]


@pytest.mark.parametrize("recovery", ["success", "missing", "failed"])
async def test_failed_cleanup_recovers_or_leaves_connection_unavailable(database, recovery):
    conn, path = database
    factory = AsyncMock(wraps=conn._reconnect_fn)
    conn._reconnect_fn = factory if recovery != "missing" else None
    if recovery == "failed":
        factory.side_effect = RuntimeError("fixture fenced recovery")
    conn._conn.commit = AsyncMock(side_effect=RuntimeError("fixture commit"))
    conn._conn.rollback = AsyncMock(side_effect=RuntimeError("fixture rollback"))
    with pytest.raises(RuntimeError, match="fixture commit"):
        await conn.execute_committed("INSERT INTO sample VALUES(1)")
    assert await observed(path) == []
    if recovery == "success":
        factory.assert_awaited_once()
        assert await conn.execute_committed("INSERT INTO sample VALUES(2)") == 1
        assert await observed(path) == [2]
    else:
        with pytest.raises(ValueError, match="no active connection"):
            await conn.execute_committed("INSERT INTO sample VALUES(2)")
        assert factory.await_count == (1 if recovery == "failed" else 0)


async def test_new_quarantine_does_not_block_rollback_cleanup(database, monkeypatch):
    conn, path = database
    conn._db_path = path
    calls = []

    def gate(_path):
        calls.append(_path)
        if len(calls) == 2:
            raise RuntimeError("fixture quarantine")

    monkeypatch.setattr("genesis.db.integrity.assert_not_quarantined", gate)
    with pytest.raises(RuntimeError, match="fixture quarantine"):
        await conn.execute_committed("INSERT INTO sample VALUES(1)")
    assert calls == [path, path]
    assert not conn.in_transaction and await observed(path) == []


async def test_transient_execute_retry_materializes_parameters_once(database, monkeypatch):
    conn, path = database
    original = conn._conn.execute
    calls = 0

    async def transient(sql, params):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return await original(sql, params)

    conn._conn.execute = transient
    sleeper = AsyncMock()
    monkeypatch.setattr(connection_mod, "_async_sleep", sleeper)
    assert await conn.execute_committed("INSERT INTO sample VALUES(?)", iter([3])) == 1
    assert calls == 2 and sleeper.await_count == 1
    assert await observed(path) == [3]


@pytest.mark.parametrize("exhaust", [False, True])
async def test_commit_lock_retry_keeps_or_cleans_the_actual_write(database, monkeypatch, exhaust):
    conn, path = database
    original = conn._conn.commit
    calls = 0

    async def locked_commit():
        nonlocal calls
        calls += 1
        if exhaust or calls == 1:
            raise sqlite3.OperationalError("database is locked")
        await original()

    conn._conn.commit = locked_commit
    sleeper = AsyncMock()
    monkeypatch.setattr(connection_mod, "_async_sleep", sleeper)
    if exhaust:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await conn.execute_committed("INSERT INTO sample VALUES(4)")
        assert calls == len(connection_mod._WRITE_RETRY_DELAYS) + 1
        assert await observed(path) == []
        conn._conn.commit = original
        assert await conn.execute_committed("INSERT INTO sample VALUES(5)") == 1
        assert await observed(path) == [5]
    else:
        assert await conn.execute_committed("INSERT INTO sample VALUES(4)") == 1
        assert calls == 2 and sleeper.await_count == 1
        assert await observed(path) == [4]
    assert not conn.in_transaction
