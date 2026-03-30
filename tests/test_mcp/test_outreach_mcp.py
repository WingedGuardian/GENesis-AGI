"""Tests for outreach-mcp server — verify all tools are registered."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import genesis.mcp.outreach_mcp as mcp_mod
from genesis.mcp.outreach_mcp import mcp


async def test_all_tools_registered():
    tools = await mcp.get_tools()
    for name in ["outreach_send", "outreach_queue", "outreach_engagement",
                 "outreach_preferences", "outreach_digest",
                 "outreach_send_and_wait"]:
        assert name in tools, f"Missing tool: {name}"


async def test_outreach_send_without_pipeline():
    """Should return error string when pipeline not initialized."""
    tools = await mcp.get_tools()
    result = await tools["outreach_send"].fn(
        message="test", category="alert", channel="whatsapp"
    )
    assert "not initialized" in result.lower() or "error" in result.lower()


async def test_send_and_wait_without_pipeline():
    """Should return error when pipeline not initialized."""
    tools = await mcp.get_tools()
    result = await tools["outreach_send_and_wait"].fn(message="test")
    assert "not initialized" in result.lower()


@pytest.mark.asyncio
async def test_send_and_wait_success():
    """Should return reply text from pipeline."""
    mock_result = MagicMock()
    mock_result.outreach_id = "out-123"
    mock_result.status.value = "delivered"

    mock_pipeline = AsyncMock()
    mock_pipeline.submit_and_wait = AsyncMock(return_value=(mock_result, "user said yes"))

    old_pipeline = mcp_mod._pipeline
    try:
        mcp_mod._pipeline = mock_pipeline
        tools = await mcp.get_tools()
        result = await tools["outreach_send_and_wait"].fn(
            message="Do you approve?", category="blocker", channel="telegram",
        )
        data = json.loads(result)
        assert data["reply"] == "user said yes"
        assert data["timed_out"] is False
        assert data["status"] == "delivered"
    finally:
        mcp_mod._pipeline = old_pipeline


@pytest.mark.asyncio
async def test_send_and_wait_timeout():
    """Should indicate timeout when reply is None."""
    mock_result = MagicMock()
    mock_result.outreach_id = "out-456"
    mock_result.status.value = "delivered"

    mock_pipeline = AsyncMock()
    mock_pipeline.submit_and_wait = AsyncMock(return_value=(mock_result, None))

    old_pipeline = mcp_mod._pipeline
    try:
        mcp_mod._pipeline = mock_pipeline
        tools = await mcp.get_tools()
        result = await tools["outreach_send_and_wait"].fn(
            message="Are you there?", timeout_seconds=5,
        )
        data = json.loads(result)
        assert data["reply"] is None
        assert data["timed_out"] is True
    finally:
        mcp_mod._pipeline = old_pipeline


async def test_send_and_wait_invalid_category():
    """Should return error for invalid category."""
    mock_pipeline = AsyncMock()
    old_pipeline = mcp_mod._pipeline
    try:
        mcp_mod._pipeline = mock_pipeline
        tools = await mcp.get_tools()
        result = await tools["outreach_send_and_wait"].fn(
            message="test", category="nonexistent",
        )
        assert "invalid category" in result.lower()
    finally:
        mcp_mod._pipeline = old_pipeline
