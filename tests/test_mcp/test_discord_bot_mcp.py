"""Tests for discord-bot MCP server — fetch_messages, fetch_forum_threads, send_reply."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import genesis.mcp.discord_bot_mcp as bot_mod
from genesis.mcp.discord_bot_mcp import mcp


@pytest.mark.asyncio
async def test_all_tools_registered():
    """All three Discord tools should be registered."""
    tools = {tool.name: tool for tool in await mcp.list_tools()}
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
            tools = {tool.name: tool for tool in await mcp.list_tools()}
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
            tools = {tool.name: tool for tool in await mcp.list_tools()}
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
        tools = {tool.name: tool for tool in await mcp.list_tools()}
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
            tools = {tool.name: tool for tool in await mcp.list_tools()}
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
            tools = {tool.name: tool for tool in await mcp.list_tools()}
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
            tools = {tool.name: tool for tool in await mcp.list_tools()}
            result = await tools["send_reply"].fn(
                channel_id="456", content=long_message,
            )

        data = json.loads(result)
        assert data["status"] == "sent"
        # Should have been split into at least 2 chunks
        assert mock_client.post.call_count >= 2
    finally:
        bot_mod._bot_token = old_token


# ── Egress boundary ────────────────────────────────────────────────────


@pytest.fixture
def reply_transport(monkeypatch):
    monkeypatch.setattr(bot_mod, "_bot_token", "test-token")
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = MagicMock(status_code=200)
    client.post.return_value.json.return_value = {"id": "sent-1"}
    with (
        patch.object(bot_mod.httpx, "AsyncClient", return_value=client) as factory,
        patch.object(bot_mod, "_chunk_text", wraps=bot_mod._chunk_text) as chunk,
        patch("genesis.autonomy.shadow_gate.observe_discord_send") as observe,
    ):
        yield client, factory, chunk, observe


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    "sk-" + "synthetic" * 4,
    "Internal host: 192.168.1.42",
    "Read /home/example/private.txt",
    "Use 4242 4242 4242 4242",
    "Safe prefix " * 200 + "\nAPI_TOKEN=synthetic-not-a-real-secret",
])
async def test_send_reply_egress_refuses_before_transport(content, reply_transport, caplog):
    client, factory, chunk, observe = reply_transport
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    result = await tools["send_reply"].fn(channel_id="456", content=content)

    error = json.loads(result)["error"]
    if content == "Use 4242 4242 4242 4242":
        assert error == "send_reply: refused, the message contains a payment card number."
    else:
        assert error == "send_reply: refused, the message contains sensitive content."
    assert content not in result
    assert content not in caplog.text
    factory.assert_not_called()
    client.post.assert_not_called()
    chunk.assert_not_called()
    observe.assert_not_called()


@pytest.mark.asyncio
async def test_send_reply_egress_failure_is_value_free(reply_transport, caplog):
    client, factory, chunk, observe = reply_transport
    secret = "synthetic-private-value"
    with patch("genesis.content.egress.scan_outbound", side_effect=RuntimeError(secret)):
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        result = await tools["send_reply"].fn(channel_id="456", content="Hello!")

    assert "refused" in json.loads(result)["error"]
    assert secret not in result + caplog.text
    factory.assert_not_called()
    client.post.assert_not_called()
    chunk.assert_not_called()
    observe.assert_not_called()


@pytest.mark.asyncio
async def test_send_reply_scrubbed_text_is_chunked_and_observed(reply_transport):
    client, _, chunk, observe = reply_transport
    content = "Hello — world! " * 180
    expected = "Hello--world! " * 180
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    result = await tools["send_reply"].fn(
        channel_id="456", content=content, reply_to="msg-789",
    )

    assert json.loads(result)["status"] == "sent"
    chunk.assert_called_once_with(expected)
    payloads = [call.kwargs["json"] for call in client.post.call_args_list]
    assert len(payloads) > 1
    assert "".join(payload["content"] for payload in payloads) == expected
    assert all(len(payload["content"]) <= 2000 for payload in payloads)
    assert payloads[0]["message_reference"]["message_id"] == "msg-789"
    assert all("message_reference" not in payload for payload in payloads[1:])
    observe.assert_awaited_once()
    assert observe.call_args.kwargs["content"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500])
async def test_send_reply_later_http_error_preserves_sent_ids(status, reply_transport):
    client, _, _, observe = reply_transport
    failure = MagicMock(status_code=status, text="unavailable")
    failure.json.return_value = {"retry_after": 3}
    client.post.side_effect = [client.post.return_value, failure]
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    result = await tools["send_reply"].fn(channel_id="456", content="A" * 5000)

    data = json.loads(result)
    assert "error" in data
    assert data["sent_ids"] == ["sent-1"]
    assert client.post.call_count == 2
    observe.assert_not_called()


def test_community_responder_profile_exists():
    """community-responder should be a valid session profile (renamed from discord-monitor)."""
    from genesis.cc.direct_session import VALID_PROFILES

    assert "community-responder" in VALID_PROFILES
    # The old campaign-coupled name must be fully gone from tracked source.
    assert "discord-monitor" not in VALID_PROFILES


def test_community_responder_mcp_profile_exists():
    """community-responder should have an MCP profile with the discord-bot server."""
    from genesis.cc.session_config import _MCP_PROFILES

    assert "community-responder" in _MCP_PROFILES
    assert "discord-monitor" not in _MCP_PROFILES
    servers = _MCP_PROFILES["community-responder"]
    assert "discord-bot" in servers
    assert "genesis-health" in servers
    assert "genesis-outreach" in servers
    # Should NOT include genesis-memory (reactive doesn't need it)
    assert "genesis-memory" not in servers
