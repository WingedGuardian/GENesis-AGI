"""Tests for SerializedConnection lock error tracking and recovery (A3).

WS-1 PR-1 semantics: every SQL call retries lock errors in-place (bounded,
jittered) before surfacing them, so the consecutive-error counter counts
exhausted retry EPISODES — one per failed call — not raw underlying attempts.
Reconnect-after-N still works, at episode granularity.
"""

import sqlite3

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
