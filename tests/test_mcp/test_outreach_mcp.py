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


# ── standalone category validation tests ────────────────────────────────


@pytest.mark.asyncio
async def test_send_standalone_invalid_category():
    """Standalone path should reject invalid categories before enqueuing."""
    mock_db = AsyncMock()
    old_pipeline, old_db = mcp_mod._pipeline, mcp_mod._db
    try:
        mcp_mod._pipeline = None
        mcp_mod._db = mock_db
        tools = await mcp.get_tools()
        result = await tools["outreach_send"].fn(
            message="Test", category="discord", channel="discord",
        )
        data = json.loads(result)
        assert "error" in data
        assert "Invalid category" in data["error"]
        assert "discord" in data["error"]
        # Verify DB was NOT called (message not enqueued)
        mock_db.execute.assert_not_called()
    finally:
        mcp_mod._pipeline = old_pipeline
        mcp_mod._db = old_db


@pytest.mark.asyncio
async def test_send_standalone_valid_category():
    """Standalone path should accept valid categories and enqueue."""
    old_pipeline, old_db = mcp_mod._pipeline, mcp_mod._db
    try:
        mcp_mod._pipeline = None
        mcp_mod._db = AsyncMock()
        with patch("genesis.db.crud.pending_outreach.ensure_table", new_callable=AsyncMock), \
             patch("genesis.db.crud.pending_outreach.enqueue", new_callable=AsyncMock, return_value="pending-123"):
            tools = await mcp.get_tools()
            result = await tools["outreach_send"].fn(
                message="Test post", category="content", channel="discord",
            )
        data = json.loads(result)
        assert data["status"] == "queued"
        assert data["pending_id"] == "pending-123"
    finally:
        mcp_mod._pipeline = old_pipeline
        mcp_mod._db = old_db


@pytest.mark.asyncio
async def test_send_standalone_email_resolves_and_enqueues_thread_recipient():
    """Fallback (pipeline=None) MUST resolve the thread's recipient and carry
    both thread_id + validated_recipient into enqueue — otherwise the queued
    email is recipient-less and the drain self-sends to the agent's own address.
    """
    old_pipeline, old_db = mcp_mod._pipeline, mcp_mod._db
    enq = AsyncMock(return_value="pending-xyz")
    try:
        mcp_mod._pipeline = None
        mcp_mod._db = AsyncMock()
        with patch("genesis.db.crud.pending_outreach.ensure_table", new_callable=AsyncMock), \
             patch("genesis.db.crud.pending_outreach.enqueue", enq), \
             patch("genesis.db.crud.email_threads.get_thread", new_callable=AsyncMock,
                   return_value={"recipient": "real@prospect.com"}):
            tools = await mcp.get_tools()
            result = await tools["outreach_send"].fn(
                message="following up", category="notification", channel="email",
                thread_id="t1",
            )
        assert json.loads(result)["status"] == "queued"
        kwargs = enq.call_args.kwargs
        assert kwargs["thread_id"] == "t1"
        assert kwargs["validated_recipient"] == "real@prospect.com"
    finally:
        mcp_mod._pipeline = old_pipeline
        mcp_mod._db = old_db


@pytest.mark.asyncio
async def test_send_standalone_email_without_thread_enqueues_no_recipient():
    """A queued email with no thread_id carries validated_recipient=None so the
    drain's self-send guard drops it (never silently self-sends)."""
    old_pipeline, old_db = mcp_mod._pipeline, mcp_mod._db
    enq = AsyncMock(return_value="pending-none")
    try:
        mcp_mod._pipeline = None
        mcp_mod._db = AsyncMock()
        with patch("genesis.db.crud.pending_outreach.ensure_table", new_callable=AsyncMock), \
             patch("genesis.db.crud.pending_outreach.enqueue", enq):
            tools = await mcp.get_tools()
            await tools["outreach_send"].fn(
                message="orphan", category="notification", channel="email",
            )
        kwargs = enq.call_args.kwargs
        assert kwargs["thread_id"] is None
        assert kwargs["validated_recipient"] is None
    finally:
        mcp_mod._pipeline = old_pipeline
        mcp_mod._db = old_db
