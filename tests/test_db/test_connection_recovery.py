"""Tests for SerializedConnection lock error tracking and recovery (A3)."""

import sqlite3

import aiosqlite
import pytest

from genesis.db.connection import SerializedConnection


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


async def test_error_counter_increments_on_lock(recovery_conn):
    """Lock errors increment the counter and re-raise."""
    # Patch the underlying execute to simulate lock error
    original = recovery_conn._conn.execute

    call_count = 0

    async def fake_execute(sql, params=None):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise sqlite3.OperationalError("database is locked")
        return await original(sql, params)

    recovery_conn._conn.execute = fake_execute

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        await recovery_conn.execute("SELECT 1")

    assert recovery_conn._consecutive_errors == 1

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        await recovery_conn.execute("SELECT 1")

    assert recovery_conn._consecutive_errors == 2


async def test_reconnect_after_threshold(recovery_conn):
    """After _MAX_LOCK_ERRORS consecutive failures, reconnection is attempted."""
    recovery_conn._max_errors = 3  # Lower threshold for testing
    original_conn = recovery_conn._conn

    fail_count = 0

    async def always_fail(sql, params=None):
        nonlocal fail_count
        fail_count += 1
        raise sqlite3.OperationalError("database is locked")

    recovery_conn._conn.execute = always_fail

    # First 2 failures — just increment counter
    for _ in range(2):
        with pytest.raises(sqlite3.OperationalError):
            await recovery_conn.execute("SELECT 1")

    assert recovery_conn._consecutive_errors == 2

    # 3rd failure — triggers reconnect
    with pytest.raises(sqlite3.OperationalError):
        await recovery_conn.execute("SELECT 1")

    # After reconnect, counter resets and conn is new
    assert recovery_conn._consecutive_errors == 0
    assert recovery_conn._conn is not original_conn


async def test_no_reconnect_without_fn():
    """Without reconnect_fn, lock errors just increment and re-raise."""
    conn = await aiosqlite.connect(":memory:")
    sc = SerializedConnection(conn)  # No reconnect_fn


    async def fake_fail(sql, params=None):
        raise sqlite3.OperationalError("database is locked")

    conn.execute = fake_fail

    for _ in range(10):
        with pytest.raises(sqlite3.OperationalError):
            await sc.execute("SELECT 1")

    # Counter goes up but no crash
    assert sc._consecutive_errors == 10
    await sc.close()
