"""Tests for outreach MCP tool implementations."""

import pytest

from genesis.mcp.outreach_mcp import mcp


@pytest.mark.asyncio
async def test_tools_registered():
    tools = await mcp.get_tools()
    expected = {"outreach_send", "outreach_queue", "outreach_engagement",
                "outreach_preferences", "outreach_digest"}
    assert expected.issubset(set(tools.keys()))


@pytest.mark.asyncio
async def test_outreach_send_without_pipeline():
    tools = await mcp.get_tools()
    result = await tools["outreach_send"].fn(
        message="Test", category="surplus", channel="telegram",
    )
    assert "not initialized" in result.lower() or "error" in result.lower()
