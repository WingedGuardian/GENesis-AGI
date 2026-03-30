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
