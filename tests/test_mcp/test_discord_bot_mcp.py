"""Tests for discord-bot MCP server — fetch_messages, fetch_forum_threads, send_reply."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import genesis.mcp.discord_bot_mcp as bot_mod
from genesis.mcp.discord_bot_mcp import mcp


async def test_all_tools_registered():
    """All three Discord tools should be registered."""
    tools = await mcp.get_tools()
    for name in ["fetch_messages", "fetch_forum_threads", "send_reply"]:
        assert name in tools, f"Missing tool: {name}"


# ── fetch_messages ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_messages_success():
    """Should return formatted messages oldest-first."""
    mock_messages = [
        {
            "id": "2",
            "author": {"id": "u1", "username": "alice", "bot": False},
            "content": "Newer message",
            "timestamp": "2026-06-13T12:00:00Z",
        },
        {
            "id": "1",
            "author": {"id": "u2", "username": "bob", "bot": True},
            "content": "Older message",
            "timestamp": "2026-06-13T11:00:00Z",
        },
    ]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_messages

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    old_token = bot_mod._bot_token
    try:
        bot_mod._bot_token = "test-token"
        with patch("genesis.mcp.discord_bot_mcp.httpx.AsyncClient", return_value=mock_client):
            tools = await mcp.get_tools()
            result = await tools["fetch_messages"].fn(channel_id="123", limit=10)

        data = json.loads(result)
        assert data["count"] == 2
        # Should be oldest-first (reversed from API response)
        assert data["messages"][0]["id"] == "1"
        assert data["messages"][0]["author_name"] == "bob"
        assert data["messages"][0]["is_bot"] is True
        assert data["messages"][1]["id"] == "2"
        assert data["messages"][1]["author_name"] == "alice"
    finally:
        bot_mod._bot_token = old_token


@pytest.mark.asyncio
async def test_fetch_messages_rate_limited():
    """Should return error with retry_after on 429."""
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.json.return_value = {"retry_after": 5.0}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    old_token = bot_mod._bot_token
    try:
        bot_mod._bot_token = "test-token"
        with patch("genesis.mcp.discord_bot_mcp.httpx.AsyncClient", return_value=mock_client):
            tools = await mcp.get_tools()
            result = await tools["fetch_messages"].fn(channel_id="123")

        data = json.loads(result)
        assert "error" in data
        assert "Rate limited" in data["error"]
    finally:
        bot_mod._bot_token = old_token


@pytest.mark.asyncio
async def test_fetch_messages_no_token():
    """Should return error when bot token not configured."""
    old_token = bot_mod._bot_token
    try:
        bot_mod._bot_token = None
        tools = await mcp.get_tools()
        result = await tools["fetch_messages"].fn(channel_id="123")

        data = json.loads(result)
        assert "error" in data
        assert "not configured" in data["error"]
    finally:
        bot_mod._bot_token = old_token


# ── fetch_forum_threads ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_forum_threads_success():
    """Should return threads with their messages."""
    mock_active_resp = MagicMock()
    mock_active_resp.status_code = 200
    mock_active_resp.json.return_value = {
        "threads": [
            {"id": "t1", "name": "Bug: crash on startup", "parent_id": "forum1", "message_count": 3},
            {"id": "t2", "name": "Unrelated thread", "parent_id": "other", "message_count": 1},
        ],
    }

    mock_archived_resp = MagicMock()
    mock_archived_resp.status_code = 200
    mock_archived_resp.json.return_value = {"threads": []}

    mock_thread_msgs = MagicMock()
    mock_thread_msgs.status_code = 200
    mock_thread_msgs.json.return_value = [
        {
            "id": "m1",
            "author": {"id": "u1", "username": "reporter", "bot": False},
            "content": "App crashes on startup",
            "timestamp": "2026-06-13T10:00:00Z",
        },
    ]

    mock_client = AsyncMock()

    async def mock_get(url, **kwargs):
        if "threads/active" in url:
            return mock_active_resp
        if "threads/archived" in url:
            return mock_archived_resp
        return mock_thread_msgs

    mock_client.get = mock_get
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    old_token = bot_mod._bot_token
    try:
        bot_mod._bot_token = "test-token"
        with patch("genesis.mcp.discord_bot_mcp.httpx.AsyncClient", return_value=mock_client):
            tools = await mcp.get_tools()
            result = await tools["fetch_forum_threads"].fn(channel_id="forum1")

        data = json.loads(result)
        # Should only include the thread from the target forum (parent_id="forum1")
        assert data["count"] == 1
        assert data["threads"][0]["name"] == "Bug: crash on startup"
        assert len(data["threads"][0]["messages"]) == 1
        assert data["threads"][0]["messages"][0]["author_name"] == "reporter"
    finally:
        bot_mod._bot_token = old_token


# ── send_reply ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_reply_success():
    """Should POST message and return sent ID."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "sent-123"}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    old_token = bot_mod._bot_token
    try:
        bot_mod._bot_token = "test-token"
        with patch("genesis.mcp.discord_bot_mcp.httpx.AsyncClient", return_value=mock_client):
            tools = await mcp.get_tools()
            result = await tools["send_reply"].fn(
                channel_id="456", content="Hello!", reply_to="msg-789",
            )

        data = json.loads(result)
        assert data["status"] == "sent"
        assert "sent-123" in data["sent_ids"]

        # Verify POST payload included message_reference
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        assert payload["content"] == "Hello!"
        assert payload["message_reference"]["message_id"] == "msg-789"
    finally:
        bot_mod._bot_token = old_token


@pytest.mark.asyncio
async def test_send_reply_auto_chunks():
    """Should split long messages into multiple chunks."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "sent-1"}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    long_message = "A" * 3000  # Exceeds 2000-char limit

    old_token = bot_mod._bot_token
    try:
        bot_mod._bot_token = "test-token"
        with patch("genesis.mcp.discord_bot_mcp.httpx.AsyncClient", return_value=mock_client):
            tools = await mcp.get_tools()
            result = await tools["send_reply"].fn(
                channel_id="456", content=long_message,
            )

        data = json.loads(result)
        assert data["status"] == "sent"
        # Should have been split into at least 2 chunks
        assert mock_client.post.call_count >= 2
    finally:
        bot_mod._bot_token = old_token


# ── Profile tests ───────────────────────────────────────────────────────


def test_discord_monitor_profile_exists():
    """discord-monitor should be a valid session profile."""
    from genesis.cc.direct_session import VALID_PROFILES

    assert "discord-monitor" in VALID_PROFILES


def test_discord_monitor_mcp_profile_exists():
    """discord-monitor should have an MCP profile with discord-bot server."""
    from genesis.cc.session_config import _MCP_PROFILES

    assert "discord-monitor" in _MCP_PROFILES
    servers = _MCP_PROFILES["discord-monitor"]
    assert "discord-bot" in servers
    assert "genesis-health" in servers
    assert "genesis-outreach" in servers
    # Should NOT include genesis-memory (reactive doesn't need it)
    assert "genesis-memory" not in servers
