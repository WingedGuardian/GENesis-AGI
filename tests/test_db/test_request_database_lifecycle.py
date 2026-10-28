"""Connection ownership under real SQLite work and injected lifecycle failures."""

import asyncio
import os
import sqlite3
import sys
import threading
from pathlib import Path

import aiosqlite
import anyio
import pytest

from genesis.db import request_database
from genesis.db.connection import ConnectionCleanupError, get_db
from genesis.db.request_database import DatabaseScopeError, RequestDatabase

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def db(tmp_path):
    path = tmp_path / "scratch.db"
    setup = await get_db(path)
    await setup.execute("CREATE TABLE events (value INTEGER)")
    await setup.commit()
    await setup.close()
    manager = RequestDatabase(path, max_units=1, checkout_timeout_s=0.05)
    yield manager
    await manager.close()


def values(db):
    with sqlite3.connect(db.path) as reader:
        return [row[0] for row in reader.execute("SELECT value FROM events ORDER BY value")]


async def test_explicit_commit_survives_error_but_tail_does_not(db):
    with pytest.raises(ValueError):
        async with db.unit_of_work():
            await db.execute("INSERT INTO events VALUES (1)")
            await db.commit()
            await db.execute("INSERT INTO events VALUES (2)")
            raise ValueError("tail fails")
    assert values(db) == [1]


async def test_nested_and_joined_work_share_unit_and_expire(db):
    release = asyncio.Event()

    async def late_child():
        await release.wait()
        with pytest.raises(DatabaseScopeError):
            await db.execute("INSERT INTO events VALUES (99)")

    with pytest.raises(DatabaseScopeError):
        await db.execute("SELECT 1")
    async with db.unit_of_work():
        first = db._lease().connection
        async with db.unit_of_work():
            assert db._lease().connection is first
        await asyncio.create_task(db.execute_fetchall("SELECT 1"))
        cursor = await db.execute("SELECT 1")
        saved_fetch = cursor.fetchone
        child = asyncio.create_task(late_child())
    with pytest.raises(DatabaseScopeError):
        await saved_fetch()
    with pytest.raises(DatabaseScopeError):
        cursor.arraysize = 2
    release.set()
    await child
    async with db.unit_of_work():
        assert db._lease().connection is not first


async def test_capacity_timeout_and_shutdown_waiters(db):
    entered = asyncio.Event()

    async def hold():
        async with db.unit_of_work():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await entered.wait()
    with pytest.raises(TimeoutError):
        async with db.unit_of_work():
            pytest.fail("Admission exceeded configured limit")
    waiter = asyncio.create_task(hold())
    await asyncio.sleep(0)
    await db.close()
    assert task.cancelled() and waiter.cancelled()
    assert not db._owners and not db._workers
    with pytest.raises(DatabaseScopeError):
        async with db.unit_of_work():
            pass


@pytest.mark.parametrize("phase", ["connect", "configure"])
async def test_cancel_during_open_drains_eventual_handle(db, monkeypatch, phase):
    arrived, release = asyncio.Event(), asyncio.Event()
    opened = []
    method = "_connect" if phase == "connect" else "execute"
    original = getattr(aiosqlite.Connection, method)

    async def delayed(connection, *args, **kwargs):
        result = await original(connection, *args, **kwargs)
        if not opened:
            opened.append(connection)
            arrived.set()
            await release.wait()
        return result

    monkeypatch.setattr(aiosqlite.Connection, method, delayed)

    async def enter():
        async with db.unit_of_work():
            pytest.fail("Cancelled admission reached its body")

    task = asyncio.create_task(enter())
    await arrived.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert opened[0]._connection is None
    async with db.unit_of_work():
        assert (await db.execute_fetchall("SELECT 42"))[0][0] == 42


async def test_anyio_level_cancellation_rolls_back(db):
    with anyio.CancelScope() as scope:
        async with db.unit_of_work():
            await db.execute("INSERT INTO events VALUES (1)")
            scope.cancel()
            await anyio.sleep(0)
    assert values(db) == []
    assert not db._workers and not db._owners
    async with db.unit_of_work():
        await db.execute("SELECT 1")


async def test_repeated_cancel_during_close_retains_capacity(db, monkeypatch):
    closing, release = asyncio.Event(), asyncio.Event()

    async def work():
        async with db.unit_of_work():
            conn = db._lease().connection
            original = conn._conn.close

            async def close():
                closing.set()
                await release.wait()
                await original()

            monkeypatch.setattr(conn._conn, "close", close)
            await db.execute("INSERT INTO events VALUES (1)")

    task = asyncio.create_task(work())
    await closing.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(TimeoutError):
        async with db.unit_of_work():
            pass
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The commit was already completed before cancellation: no replay/rollback
    # claim is made for lost replies after a queued commit.
    assert values(db) == [1]


async def test_commit_failure_propagates_and_closes(db, monkeypatch):
    async def fail():
        raise sqlite3.OperationalError("injected commit failure")

    with pytest.raises(sqlite3.OperationalError, match="injected"):
        async with db.unit_of_work():
            conn = db._lease().connection
            raw = conn._conn
            monkeypatch.setattr(conn._conn, "commit", fail)
            await db.execute("INSERT INTO events VALUES (1)")
    assert raw._connection is None
    assert values(db) == []


async def test_close_failure_retains_capacity_and_fails_shutdown(tmp_path, monkeypatch):
    path = tmp_path / "failure.db"
    sqlite3.connect(path).close()
    manager = RequestDatabase(path, max_units=1, checkout_timeout_s=0.01)
    original = None

    async def fail():
        raise OSError("injected close failure")

    try:
        with pytest.raises(OSError, match="injected"):
            async with manager.unit_of_work():
                conn = manager._lease().connection
                original = conn._conn.close
                monkeypatch.setattr(conn._conn, "close", fail)
        with pytest.raises(TimeoutError):
            async with manager.unit_of_work():
                pass
        with pytest.raises(DatabaseScopeError, match="unconfirmed"):
            await manager.close()
    finally:
        if original:
            await original()


async def test_failed_partial_open_does_not_claim_capacity_recovered(tmp_path, monkeypatch):
    manager = RequestDatabase(tmp_path / "unused", max_units=1, checkout_timeout_s=0.01)

    async def fail(*args, **kwargs):
        raise ConnectionCleanupError("partial connection could not be closed")

    monkeypatch.setattr(request_database, "get_db", fail)
    with pytest.raises(ConnectionCleanupError):
        async with manager.unit_of_work():
            pass
    with pytest.raises(TimeoutError):
        async with manager.unit_of_work():
            pass
    with pytest.raises(DatabaseScopeError, match="unconfirmed"):
        await manager.close()


async def test_missing_database_never_created(tmp_path):
    path = tmp_path / "absent" / "db.sqlite"
    manager = RequestDatabase(path, max_units=1)
    with pytest.raises(sqlite3.OperationalError):
        async with manager.unit_of_work():
            pass
    assert not path.exists() and not path.parent.exists()
    await manager.close()


async def test_partial_cursor_releases_wal_at_scope_exit(db):
    async with db.unit_of_work():
        await db.executemany("INSERT INTO events VALUES (?)", [(x,) for x in range(100)])
    writer = await get_db(db.path)
    try:
        async with db.unit_of_work():
            cur = await db.execute("SELECT value FROM events")
            assert (await cur.fetchone())[0] == 0
            await writer.execute("INSERT INTO events VALUES (101)")
            await writer.commit()
            checkpoint = await writer.execute_fetchall("PRAGMA wal_checkpoint(PASSIVE)")
            assert checkpoint[0][1] > checkpoint[0][2]
        checkpoint = await writer.execute_fetchall("PRAGMA wal_checkpoint(PASSIVE)")
        assert checkpoint[0][1] == checkpoint[0][2]
    finally:
        await writer.close()


@pytest.mark.parametrize("phase", ["execute", "fetch"])
async def test_cancel_sql_wait_drains_worker_before_releasing_slot(db, phase):
    arrived = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    calls = 0

    def pause(value):
        nonlocal calls
        calls += 1
        if calls == (1 if phase == "execute" else 2):
            loop.call_soon_threadsafe(arrived.set)
            release.wait()
        return value

    async def work():
        async with db.unit_of_work():
            await db._lease().connection._conn.create_function("pause", 1, pause)
            await db.executemany("INSERT INTO events VALUES (?)", [(1,), (2,), (3,)])
            cursor = await db.execute("SELECT pause(value) FROM events")
            await cursor.fetchall()

    task = asyncio.create_task(work())
    try:
        await arrived.wait()
        task.cancel()
        await asyncio.sleep(0)
        with pytest.raises(TimeoutError):
            async with db.unit_of_work():
                pass
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert values(db) == []
    async with db.unit_of_work():
        await db.execute("SELECT 1")


async def test_process_death_preserves_commit_and_discards_tail(db):
    code = """
import asyncio, sys
from genesis.db.request_database import RequestDatabase
async def run():
    db = RequestDatabase(sys.argv[1], max_units=1)
    async with db.unit_of_work():
        await db.execute('INSERT INTO events VALUES (1)')
        await db.commit()
        await db.execute('INSERT INTO events VALUES (2)')
        print('ready', flush=True)
        await asyncio.Event().wait()
asyncio.run(run())
"""
    env = dict(os.environ, PYTHONPATH=str(Path(request_database.__file__).parents[2]))
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, str(db.path), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await child.stdout.readline() == b"ready\n"
    finally:
        if child.returncode is None:
            child.kill()
        await child.communicate()
    assert values(db) == [1]


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
async def test_invalid_capacity_rejected(tmp_path, value):
    with pytest.raises(ValueError, match="max_units"):
        RequestDatabase(tmp_path / "unused", max_units=value)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
async def test_invalid_checkout_timeout_rejected(tmp_path, value):
    with pytest.raises(ValueError, match="checkout_timeout"):
        RequestDatabase(tmp_path / "unused", max_units=1, checkout_timeout_s=value)


@pytest.mark.parametrize("factory", ["execute", "cursor", "executemany", "executescript"])
@pytest.mark.parametrize("method", ["execute", "executemany", "executescript"])
async def test_cursor_properties_and_execute_return_stay_scoped(db, factory, method):
    async with db.unit_of_work():
        if factory == "execute":
            cursor = await db.execute("SELECT 1")
        elif factory == "cursor":
            cursor = await db.cursor()
        elif factory == "executemany":
            cursor = await db.executemany("INSERT INTO events VALUES (?)", [(1,)])
        else:
            cursor = await db.executescript("SELECT 1;")
        assert cursor.row_factory is sqlite3.Row
        cursor.row_factory = cursor.row_factory
        with pytest.raises(DatabaseScopeError, match="Raw cursor"):
            _ = cursor.connection
        if method == "execute":
            returned = await cursor.execute("SELECT 1")
        elif method == "executemany":
            returned = await cursor.executemany("INSERT INTO events VALUES (?)", [(42,)])
        else:
            returned = await cursor.executescript("SELECT 1;")
        assert returned is cursor
        await cursor.execute("SELECT 42")
        saved = cursor.fetchone
        assert (await saved())[0] == 42
    with pytest.raises(DatabaseScopeError):
        await saved()


async def test_close_from_joined_child_refuses_before_shutdown(db, monkeypatch):
    async def forbidden_shutdown():
        pytest.fail("Joined child must not initiate shutdown of its parent")

    monkeypatch.setattr(db, "_shutdown", forbidden_shutdown)
    async with db.unit_of_work():
        with pytest.raises(DatabaseScopeError):
            await asyncio.create_task(db.close())
    # Restore so the fixture can exercise ordinary outside-scope shutdown.
    monkeypatch.undo()


async def test_failed_open_drains_stop_queue_before_return(tmp_path, monkeypatch):
    arrived, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    stop_name = "stop" if hasattr(aiosqlite.Connection, "stop") else "_stop_running"
    original = getattr(aiosqlite.Connection, stop_name)
    opened = []

    def stop(connection):
        opened.append(connection)
        def block():
            loop.call_soon_threadsafe(arrived.set)
            release.wait()
        connection._tx.put_nowait((loop.create_future(), block))
        return original(connection)

    monkeypatch.setattr(aiosqlite.Connection, stop_name, stop)
    manager = RequestDatabase(tmp_path / "missing", max_units=1)

    async def open_missing():
        with pytest.raises(sqlite3.OperationalError):
            async with manager.unit_of_work():
                pass

    task = asyncio.create_task(open_missing())
    try:
        await arrived.wait()
        done, _ = await asyncio.wait({task}, timeout=0.05)
        assert not done, "Failed open released capacity before its worker stopped"
    finally:
        release.set()
        await task
        await manager.close()
        for connection in opened:
            await asyncio.to_thread(getattr(connection, "_thread", connection).join)
