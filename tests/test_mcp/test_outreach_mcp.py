"""Tests for outreach-mcp server — verify all tools are registered."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import genesis.mcp.outreach_mcp as mcp_mod
from genesis.mcp.outreach_mcp import mcp


async def test_all_tools_registered():
    tools = await mcp.get_tools()
    for name in ["outreach_send", "outreach_poll", "outreach_queue",
                 "outreach_engagement", "outreach_preferences",
                 "outreach_digest", "outreach_send_and_wait"]:
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


# ── outreach_poll tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outreach_poll_no_webhook():
    """Should return error when no webhook env var is set."""
    tools = await mcp.get_tools()
    with patch.dict("os.environ", {}, clear=True):
        result = await tools["outreach_poll"].fn(
            channel="announcements",
            question="Test?",
            answers=["A", "B"],
        )
    data = json.loads(result)
    assert "error" in data
    assert "No webhook URL" in data["error"]


@pytest.mark.asyncio
async def test_outreach_poll_success():
    """Should POST poll payload and return message_id."""
    tools = await mcp.get_tools()

    # httpx Response is sync — use MagicMock, not AsyncMock
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "poll-msg-999"}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    env = {"DISCORD_WEBHOOK_ANNOUNCEMENTS": "https://discord.com/api/webhooks/123/tok"}
    with patch.dict("os.environ", env, clear=False), \
         patch("genesis.mcp.outreach_mcp.httpx.AsyncClient", return_value=mock_client):
        result = await tools["outreach_poll"].fn(
            channel="announcements",
            question="What do you think?",
            answers=["Option A", "Option B", "Option C"],
            duration_hours=48,
        )

    data = json.loads(result)
    assert data["status"] == "created"
    assert data["message_id"] == "poll-msg-999"

    # Verify POST payload structure
    call_args = mock_client.post.call_args
    url = call_args[0][0]
    assert "123/tok" in url
    assert "wait=true" in url
    payload = call_args[1]["json"]
    assert payload["poll"]["question"]["text"] == "What do you think?"
    assert len(payload["poll"]["answers"]) == 3
    assert payload["poll"]["duration"] == 48


@pytest.mark.asyncio
async def test_outreach_poll_http_error():
    """Should return error on Discord API failure."""
    import httpx

    tools = await mcp.get_tools()

    # httpx Response is sync — use MagicMock
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.text = "Forbidden"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "403", request=MagicMock(), response=mock_response,
    )

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    env = {"DISCORD_WEBHOOK_GENERAL": "https://discord.com/api/webhooks/456/tok2"}
    with patch.dict("os.environ", env, clear=False), \
         patch("genesis.mcp.outreach_mcp.httpx.AsyncClient", return_value=mock_client):
        result = await tools["outreach_poll"].fn(
            channel="general",
            question="Test?",
            answers=["Yes", "No"],
        )

    data = json.loads(result)
    assert "error" in data
    assert "403" in data["error"]
