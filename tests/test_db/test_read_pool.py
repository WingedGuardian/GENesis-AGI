"""ReadConnectionPool + open_ro_connection (follow-up ac27b693, PR-4).

A dedicated ``mode=ro`` pool lets recall's read stages run off the shared
SerializedConnection write lock. These tests pin the load-bearing invariants:

- **checkout exclusivity** — the entire no-per-connection-lock design rests on a
  connection only ever being held by one coroutine at a time;
- **``mode=ro`` is truly read-only** — a pooled connection cannot write;
- **WAL-awareness** — a pooled reader sees committed writes (the ``immutable=1``
  trap would miss un-checkpointed WAL frames);
- **clean close/lifecycle** — idempotent close, ReadPoolClosed after close/before
  open, and no slot leak when a read errors inside the block.
"""

from __future__ import annotations

import asyncio
import sqlite3

import aiosqlite
import pytest

from genesis.db.connection import (
    ReadConnectionPool,
    ReadPoolClosed,
    get_db,
    open_ro_connection,
)


async def _seed_db(path) -> None:
    db = await aiosqlite.connect(str(path))
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    await db.execute("INSERT INTO t (id, v) VALUES (1, 'a'), (2, 'b')")
    await db.commit()
    await db.close()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "pool.db"


async def test_open_ro_connection_reads_and_row_factory(db_path):
    await _seed_db(db_path)
    conn = await open_ro_connection(db_path)
    try:
        rows = await conn.execute_fetchall("SELECT v FROM t ORDER BY id")
        assert [r[0] for r in rows] == ["a", "b"]
        # Row-factory parity with get_db: named column access must work, or the
        # RO connection silently diverges from self._db (architect SHOULD-FIX).
        named = await conn.execute_fetchall("SELECT v FROM t WHERE id = 1")
        assert named[0]["v"] == "a"
    finally:
        await conn.close()


async def test_ro_connection_cannot_write(db_path):
    """Pins the zero-write invariant — a mode=ro handle rejects INSERT."""
    await _seed_db(db_path)
    conn = await open_ro_connection(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            await conn.execute("INSERT INTO t (id, v) VALUES (3, 'c')")
    finally:
        await conn.close()


async def test_pool_acquire_reads(db_path):
    await _seed_db(db_path)
    pool = ReadConnectionPool(db_path, size=2)
    await pool.open()
    try:
        async with pool.acquire() as conn:
            rows = await conn.execute_fetchall("SELECT COUNT(*) FROM t")
            assert rows[0][0] == 2
    finally:
        await pool.close()


async def test_checkout_is_exclusive(db_path):
    """Load-bearing invariant: a checked-out connection is NEVER handed to a
    second coroutine while held. With size=1 a second acquire blocks until the
    first releases; a bug that double-hands the connection resurrects the exact
    in_transaction corruption SerializedConnection exists to prevent.
    """
    await _seed_db(db_path)
    pool = ReadConnectionPool(db_path, size=1)
    await pool.open()
    order: list[str] = []
    try:
        async with pool.acquire() as c1:
            started = asyncio.Event()

            async def second() -> None:
                started.set()
                async with pool.acquire() as c2:
                    order.append("second-acquired")
                    assert c2 is c1  # size=1 → the one connection, reused

            task = asyncio.create_task(second())
            await started.wait()
            await asyncio.sleep(0.05)  # give second() a chance to (not) acquire
            assert order == []  # still blocked behind the held checkout
            order.append("first-releasing")
        await task
        assert order == ["first-releasing", "second-acquired"]
    finally:
        await pool.close()


async def test_pool_sees_committed_writes(db_path):
    """mode=ro is WAL-aware: a write committed on the writer is visible to a
    pooled reader. (immutable=1 would miss the un-checkpointed WAL frame — the
    documented trap this pool must not fall into.)
    """
    await _seed_db(db_path)
    pool = ReadConnectionPool(db_path, size=1)
    await pool.open()
    writer = await get_db(db_path)
    try:
        await writer.execute("INSERT INTO t (id, v) VALUES (3, 'c')")
        await writer.commit()
        async with pool.acquire() as conn:
            rows = await conn.execute_fetchall("SELECT v FROM t WHERE id = 3")
            assert rows and rows[0][0] == "c"
    finally:
        await writer.close()
        await pool.close()


async def test_error_in_block_returns_connection(db_path):
    """An error inside the acquire block must still return the connection — an
    autocommit mode=ro SELECT leaves no dangling transaction, so the slot is
    safe to reuse. A leaked slot would deadlock the (size=1) pool on the next
    acquire; this test would hang instead of passing if the finally regressed.
    """
    await _seed_db(db_path)
    pool = ReadConnectionPool(db_path, size=1)
    await pool.open()
    try:
        with pytest.raises(ValueError):
            async with pool.acquire() as conn:
                await conn.execute_fetchall("SELECT 1")
                raise ValueError("boom")
        # Slot returned — this acquire would block forever if it leaked.
        async with asyncio.timeout(2):
            async with pool.acquire() as conn:
                rows = await conn.execute_fetchall("SELECT COUNT(*) FROM t")
                assert rows[0][0] == 2
    finally:
        await pool.close()


async def test_acquire_after_close_raises(db_path):
    await _seed_db(db_path)
    pool = ReadConnectionPool(db_path, size=2)
    await pool.open()
    await pool.close()
    with pytest.raises(ReadPoolClosed):
        async with pool.acquire():
            pass


async def test_acquire_before_open_raises(db_path):
    pool = ReadConnectionPool(db_path, size=2)
    with pytest.raises(ReadPoolClosed):
        async with pool.acquire():
            pass


async def test_close_is_idempotent(db_path):
    await _seed_db(db_path)
    pool = ReadConnectionPool(db_path, size=2)
    await pool.open()
    await pool.close()
    await pool.close()  # second close must not raise


async def test_open_is_idempotent(db_path):
    await _seed_db(db_path)
    pool = ReadConnectionPool(db_path, size=2)
    await pool.open()
    await pool.open()  # second open is a no-op, does not double the pool
    try:
        assert pool._queue.qsize() == 2
    finally:
        await pool.close()


async def test_size_floored_at_one(db_path):
    assert ReadConnectionPool(db_path, size=0).size == 1
    assert ReadConnectionPool(db_path, size=-3).size == 1
