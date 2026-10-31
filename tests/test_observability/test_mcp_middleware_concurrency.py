"""Concurrent tool calls sharing one MCP server connection.

The MCP SDK dispatches each request in its own task (``mcp.server.lowlevel``
``Server.run`` → ``tg.start_soon``), so two tool calls from one client can be in
flight at once, and both use the server's single long-lived
``SerializedConnection``. That proxy serializes each METHOD call but, by its own
docstring, lets two ``execute(); commit()`` sequences interleave at the method
boundary.

The middleware's per-call boundary rolls back the shared connection when a call
fails. These tests drive the REAL middleware over a REAL SerializedConnection and
pin what one call's failure may and may not do to another call that succeeded.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import aiosqlite
import pytest

from genesis.db.connection import SerializedConnection
from genesis.observability.mcp_middleware import InstrumentationMiddleware
from genesis.observability.provider_activity import ProviderActivityTracker


@pytest.fixture
async def db(tmp_path):
    """The shared connection, closed on EVERY exit. A failed assertion that
    skipped the close would leave aiosqlite's non-daemon worker thread alive,
    and the interpreter would then hang at exit instead of reporting."""
    raw = await aiosqlite.connect(str(tmp_path / "t.db"))
    await raw.execute("PRAGMA journal_mode=WAL")
    await raw.execute("CREATE TABLE t (v TEXT)")
    await raw.commit()
    conn = SerializedConnection(raw)
    try:
        yield conn
    finally:
        await conn.close()


def _ctx(name: str):
    ctx = MagicMock()
    ctx.message.name = name
    ctx.message.arguments = {}
    return ctx


async def _rows(tmp_path) -> list[str]:
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        cur = await conn.execute("SELECT v FROM t ORDER BY v")
        return [r[0] for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_a_failing_call_does_not_discard_a_concurrent_calls_write(db, tmp_path):
    """B writes and commits; A fails while B's statement is still uncommitted.

    B returns success. Its row must be durable: a client told the write
    succeeded has no reason to retry it. The interleaving is the one the
    SerializedConnection docstring names as permitted: B.execute, then A's
    boundary step, then B.commit.
    """
    mw = InstrumentationMiddleware(ProviderActivityTracker(), "memory", db=db)
    b_executed = asyncio.Event()
    a_done = asyncio.Event()

    async def call_b(_context):
        # The CRUD shape: one statement, then its own commit before returning.
        await db.execute("INSERT INTO t (v) VALUES ('b')")
        b_executed.set()
        await a_done.wait()  # the yield point where another call's boundary runs
        await db.commit()
        return {"ok": True}

    async def call_a(_context):
        await b_executed.wait()
        raise RuntimeError("a failed")

    async def run_a():
        try:
            await mw.on_call_tool(_ctx("memory_store"), call_a)
        except RuntimeError:
            pass
        finally:
            a_done.set()

    # Bounded so a broken handshake fails the test instead of hanging it.
    b_result, _ = await asyncio.wait_for(
        asyncio.gather(mw.on_call_tool(_ctx("observation_write"), call_b), run_a()),
        timeout=30,
    )

    assert b_result == {"ok": True}
    assert await _rows(tmp_path) == ["b"], (
        "call B reported success but its write was discarded by call A's failure "
        "rollback on the shared connection"
    )


@pytest.mark.asyncio
async def test_a_failing_calls_own_uncommitted_write_does_not_persist(db, tmp_path):
    """The property #1645 protects, kept: a call that FAILS after an uncommitted
    statement must not have that partial write committed by its own boundary."""
    mw = InstrumentationMiddleware(ProviderActivityTracker(), "memory", db=db)

    async def call_a(_context):
        await db.execute("INSERT INTO t (v) VALUES ('partial')")
        raise RuntimeError("a failed mid-write")

    with pytest.raises(RuntimeError):
        await mw.on_call_tool(_ctx("memory_store"), call_a)

    # A later, clean call's boundary must not sweep A's partial into a commit.
    async def call_c(_context):
        return {"ok": True}

    await mw.on_call_tool(_ctx("memory_recall"), call_c)
    assert await _rows(tmp_path) == []


@pytest.mark.asyncio
async def test_inflight_count_returns_to_zero_on_every_exit_path(db, tmp_path):
    """A leaked in-flight count would disable the failure rollback for the life
    of the server process, so every exit (success, failure, cancellation, and a
    BaseException from the tracker) must release it."""
    tracker = ProviderActivityTracker()
    mw = InstrumentationMiddleware(tracker, "memory", db=db)

    async def ok(_c):
        return 1

    async def boom(_c):
        raise RuntimeError("x")

    async def hang(_c):
        await asyncio.sleep(3600)

    await mw.on_call_tool(_ctx("memory_recall"), ok)
    with pytest.raises(RuntimeError):
        await mw.on_call_tool(_ctx("memory_store"), boom)
    task = asyncio.create_task(mw.on_call_tool(_ctx("memory_recall"), hang))
    await asyncio.sleep(0)
    assert mw._calls_in_flight == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mw._calls_in_flight == 0

    # A BaseException from the tracker record, the last step of the boundary.
    class _Abort(BaseException):
        pass

    def _raising_record(*_a, **_k):
        raise _Abort

    original_record = tracker.record
    tracker.record = _raising_record
    try:
        with pytest.raises(_Abort):
            await mw.on_call_tool(_ctx("memory_recall"), ok)
    finally:
        tracker.record = original_record
    assert mw._calls_in_flight == 0

    # A solo failure after all of that must still roll back its own partial.
    async def partial(_c):
        await db.execute("INSERT INTO t (v) VALUES ('p')")
        raise RuntimeError("partial")

    with pytest.raises(RuntimeError):
        await mw.on_call_tool(_ctx("memory_store"), partial)
    await db.commit()
    assert await _rows(tmp_path) == []
