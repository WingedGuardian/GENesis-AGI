"""Guard tests for DB read cancellation-safety (the WAL-pin root cause).

Root cause (empirically proven 2026-06-15): on a long-lived aiosqlite connection
in WAL mode, the pattern ``cursor = await db.execute(SELECT); await cursor.fetchone()``
opens a read snapshot and, if the coroutine is CANCELLED between ``execute`` and the
cursor closing, leaves the snapshot pinned — which (a) blocks the WAL checkpoint
(unbounded WAL growth) and (b) makes that connection's next write fail
``database is locked``.

The fix routes reads through ``execute_fetchall`` (or ``async with db.execute(...)``),
which never leaves a dangling cursor on the asyncio side. These tests lock in that
property so a future aiosqlite change can't silently reintroduce the leak.
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest


async def _mk(path) -> aiosqlite.Connection:
    c = await aiosqlite.connect(str(path))
    c.row_factory = aiosqlite.Row
    await c.execute("PRAGMA journal_mode=WAL")
    await c.execute("PRAGMA busy_timeout=2000")
    await c.execute("PRAGMA wal_autocheckpoint=0")  # don't auto-drain — we measure the pin
    return c


async def _seed(path) -> None:
    c = await _mk(path)
    await c.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)")
    for i in range(5):
        await c.execute("INSERT INTO t VALUES (?,?)", (f"multi{i}", str(i)))
    await c.commit()
    await c.close()


async def _advance(writer: aiosqlite.Connection, n: int = 40) -> None:
    """Push the WAL forward so a stale snapshot becomes detectable."""
    for i in range(n):
        await writer.execute("INSERT OR REPLACE INTO t VALUES (?,?)", (f"adv{i}", str(i)))
        await writer.commit()


async def _is_pinned(path) -> bool:
    """True if a TRUNCATE checkpoint is blocked by a lingering reader."""
    ck = await _mk(path)
    try:
        cur = await ck.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        busy, log, ckpt = tuple(await cur.fetchone())
        return busy == 1 or (log > 0 and ckpt < log)
    finally:
        await ck.close()


@pytest.mark.asyncio
async def test_unsafe_cursor_pattern_leaks_under_cancellation(tmp_path):
    """Characterization: the raw cursor pattern DOES pin under cancellation.

    This documents *why* the fix exists. If this ever stops reproducing, the
    cancellation-safety concern may no longer apply.
    """
    db_path = tmp_path / "leak.db"
    await _seed(db_path)
    conn = await _mk(db_path)
    writer = await _mk(db_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        cur = await conn.execute("SELECT v FROM t WHERE k LIKE 'multi%'")
        started.set()
        await release.wait()  # parked — cancelled instead of released
        await cur.fetchone()
        await cur.close()

    task = asyncio.create_task(holder())
    await started.wait()
    await _advance(writer)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _is_pinned(db_path) is True
    await writer.close()
    await conn.close()


@pytest.mark.asyncio
async def test_execute_fetchall_is_cancellation_safe(tmp_path):
    """The fix: execute_fetchall never leaves a snapshot pinned, even if cancelled."""
    db_path = tmp_path / "safe.db"
    await _seed(db_path)
    conn = await _mk(db_path)
    writer = await _mk(db_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        await conn.execute_fetchall("SELECT v FROM t WHERE k LIKE 'multi%'")
        started.set()
        await release.wait()

    task = asyncio.create_task(holder())
    await started.wait()
    await _advance(writer)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _is_pinned(db_path) is False
    # and the connection can still write
    await conn.execute("INSERT OR REPLACE INTO t VALUES ('w','1')")
    await conn.commit()
    await writer.close()
    await conn.close()


@pytest.mark.asyncio
async def test_async_with_execute_is_cancellation_safe(tmp_path):
    """async with db.execute(...) as cursor closes the cursor even on cancellation."""
    db_path = tmp_path / "ctx.db"
    await _seed(db_path)
    conn = await _mk(db_path)
    writer = await _mk(db_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with conn.execute("SELECT v FROM t WHERE k LIKE 'multi%'") as cur:
            await cur.fetchone()
            started.set()
            await release.wait()  # parked inside the context — cancellation triggers __aexit__

    task = asyncio.create_task(holder())
    await started.wait()
    await _advance(writer)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _is_pinned(db_path) is False
    await writer.close()
    await conn.close()
