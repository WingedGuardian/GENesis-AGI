"""Tests for MCP InstrumentationMiddleware."""

from __future__ import annotations

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
