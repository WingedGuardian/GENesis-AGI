"""Tests for MCP InstrumentationMiddleware."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.observability.mcp_middleware import InstrumentationMiddleware
from genesis.observability.provider_activity import ProviderActivityTracker


@pytest.mark.asyncio
class TestInstrumentationMiddleware:
    """Test the FastMCP instrumentation middleware."""

    async def test_records_successful_tool_call(self):
        tracker = ProviderActivityTracker()
        mw = InstrumentationMiddleware(tracker, "test_server")

        # Mock context with tool name
        context = MagicMock()
        context.message.name = "my_tool"

        # Mock call_next that succeeds
        call_next = AsyncMock(return_value={"result": "ok"})

        result = await mw.on_call_tool(context, call_next)

        assert result == {"result": "ok"}
        summary = tracker.summary("mcp.test_server.my_tool")
        assert summary["calls"] == 1
        assert summary["errors"] == 0
        assert summary["avg_latency_ms"] >= 0

    async def test_records_failed_tool_call(self):
        tracker = ProviderActivityTracker()
        mw = InstrumentationMiddleware(tracker, "test_server")

        context = MagicMock()
        context.message.name = "failing_tool"
        call_next = AsyncMock(side_effect=RuntimeError("tool failed"))

        with pytest.raises(RuntimeError, match="tool failed"):
            await mw.on_call_tool(context, call_next)

        summary = tracker.summary("mcp.test_server.failing_tool")
        assert summary["calls"] == 1
        assert summary["errors"] == 1

    async def test_namespace_format(self):
        """Provider name must use mcp.{server}.{tool} format."""
        tracker = ProviderActivityTracker()
        mw = InstrumentationMiddleware(tracker, "memory")

        context = MagicMock()
        context.message.name = "memory_recall"
        call_next = AsyncMock(return_value={})

        await mw.on_call_tool(context, call_next)

        summaries = tracker.summary()
        assert any(s["provider"] == "mcp.memory.memory_recall" for s in summaries)

    async def test_tracker_error_does_not_break_tool(self):
        """If tracker.record() fails, the tool call must still succeed."""
        tracker = ProviderActivityTracker()
        tracker.record = MagicMock(side_effect=RuntimeError("tracker bug"))
        mw = InstrumentationMiddleware(tracker, "test")

        context = MagicMock()
        context.message.name = "my_tool"
        call_next = AsyncMock(return_value={"ok": True})

        result = await mw.on_call_tool(context, call_next)
        assert result == {"ok": True}


@pytest.mark.asyncio
class TestMiddlewareUnitOfWork:
    """The per-call transaction boundary (WS-15 follow-up): release the read
    snapshot after each tool call so it can't pin the WAL — commit on success,
    rollback on error. DB errors never break the tool call."""

    async def test_commits_on_successful_tool_call(self):
        tracker = ProviderActivityTracker()
        db = AsyncMock()
        mw = InstrumentationMiddleware(tracker, "memory", db=db)
        context = MagicMock()
        context.message.name = "memory_recall"
        call_next = AsyncMock(return_value={"ok": True})

        result = await mw.on_call_tool(context, call_next)

        assert result == {"ok": True}
        db.commit.assert_awaited_once()
        db.rollback.assert_not_awaited()

    async def test_rolls_back_on_failed_tool_call(self):
        tracker = ProviderActivityTracker()
        db = AsyncMock()
        mw = InstrumentationMiddleware(tracker, "memory", db=db)
        context = MagicMock()
        context.message.name = "memory_store"
        call_next = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await mw.on_call_tool(context, call_next)

        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()

    async def test_no_db_is_backward_compatible(self):
        """Without a db (default), no commit/rollback is attempted."""
        tracker = ProviderActivityTracker()
        mw = InstrumentationMiddleware(tracker, "test")  # no db param
        context = MagicMock()
        context.message.name = "t"
        call_next = AsyncMock(return_value=1)

        assert await mw.on_call_tool(context, call_next) == 1

    async def test_commit_error_does_not_break_tool(self):
        """A commit failure in the boundary must not break a successful result."""
        tracker = ProviderActivityTracker()
        db = AsyncMock()
        db.commit.side_effect = RuntimeError("commit failed")
        mw = InstrumentationMiddleware(tracker, "test", db=db)
        context = MagicMock()
        context.message.name = "t"
        call_next = AsyncMock(return_value={"ok": True})

        result = await mw.on_call_tool(context, call_next)
        assert result == {"ok": True}

    async def test_rollback_error_does_not_mask_tool_error(self):
        """If rollback fails while handling a tool error, the ORIGINAL error wins."""
        tracker = ProviderActivityTracker()
        db = AsyncMock()
        db.rollback.side_effect = RuntimeError("rollback failed")
        mw = InstrumentationMiddleware(tracker, "test", db=db)
        context = MagicMock()
        context.message.name = "t"
        call_next = AsyncMock(side_effect=ValueError("original tool error"))

        with pytest.raises(ValueError, match="original tool error"):
            await mw.on_call_tool(context, call_next)

    async def test_rolls_back_on_cancelled_tool_call(self):
        """A CANCELLED tool call rolls back its partial, exactly like an errored one.

        ``asyncio.CancelledError`` is a ``BaseException``, not an ``Exception``, so
        a bare ``except Exception`` never catches it: ``success`` stays ``True`` and
        the ``finally`` COMMITS a possibly-partial write (follow-up 3183405d). The
        boundary must roll it back instead (the flip pattern defaults ``success=False``,
        so only a clean return commits).
        """
        tracker = ProviderActivityTracker()
        db = AsyncMock()
        mw = InstrumentationMiddleware(tracker, "memory", db=db)
        context = MagicMock()
        context.message.name = "memory_store"
        call_next = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await mw.on_call_tool(context, call_next)

        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()

    async def test_real_cancellation_mid_call_rolls_back(self):
        """A GENUINE task cancellation mid-tool-call rolls back, never commits.

        Unlike the synthetic ``side_effect=CancelledError`` above, this cancels a
        real task while it is suspended INSIDE ``call_next`` — exercising the
        finally's cleanup on the actual cancellation path, not just the routing.
        """
        tracker = ProviderActivityTracker()
        db = AsyncMock()
        mw = InstrumentationMiddleware(tracker, "memory", db=db)
        context = MagicMock()
        context.message.name = "memory_store"

        inside_call = asyncio.Event()

        async def _blocks_until_cancelled(_ctx):
            inside_call.set()
            await asyncio.Event().wait()  # never set — only cancellation ends this
            return {"unreachable": True}

        task = asyncio.create_task(mw.on_call_tool(context, _blocks_until_cancelled))
        await inside_call.wait()  # ensure we are suspended inside call_next
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()

    async def test_cancellation_inside_commit_still_records_the_call(self):
        """A cancellation landing IN the commit await must not swallow the record.

        `except Exception` does not catch `asyncio.CancelledError`, so before the
        DB step and the tracker record were nested in their own try/finally, a
        cancellation here left this finally early: the tool had run and the
        activity tracker never heard about it.

        Deliberately NOT asserting a rollback. CodeRabbit's finding proposed one,
        and the premise it rests on does not hold for this driver: aiosqlite
        queues the operation to its worker thread BEFORE awaiting
        (`_tx.put_nowait(...)` then `await future`), so cancelling the await
        cancels the WAIT, not the commit. MEASURED against aiosqlite 0.21 — a row
        inserted before a cancelled `commit()` is durable afterwards. A rollback
        issued here would either sit behind the queued commit as a no-op, or
        discard a successful tool call's writes.
        """
        tracker = ProviderActivityTracker()
        db = AsyncMock()

        async def _commit_that_gets_cancelled():
            raise asyncio.CancelledError()

        db.commit = AsyncMock(side_effect=_commit_that_gets_cancelled)
        mw = InstrumentationMiddleware(tracker, "memory", db=db)
        context = MagicMock()
        context.message.name = "memory_store"
        call_next = AsyncMock(return_value={"result": "ok"})

        with pytest.raises(asyncio.CancelledError):
            await mw.on_call_tool(context, call_next)

        # The cancellation propagated (above) AND the call was still recorded.
        summary = tracker.summary("mcp.memory.memory_store")
        assert summary["calls"] == 1, (
            "a cancellation inside commit() must not cost the tracker record"
        )
        assert summary["errors"] == 0, "the TOOL succeeded; only the commit wait was cut"
