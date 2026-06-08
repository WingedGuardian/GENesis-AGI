"""Tests for voice channel — sessions, handler, adapter, and API endpoint."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from genesis.channels.voice.adapter import VoiceChannelAdapter
from genesis.channels.voice.handler import VoiceConversationHandler
from genesis.channels.voice.sessions import VoiceSessionManager

# ── Session Manager ──────────────────────────────────────────────────


class TestVoiceSessionManager:
    @pytest.fixture
    def manager(self):
        return VoiceSessionManager(sustain_seconds=2, max_turns=5)

    @pytest.mark.asyncio
    async def test_create_session(self, manager):
        session = await manager.get_or_create("test-1")
        assert session.session_id == "test-1"
        assert session.turn_count == 0
        assert session.buffer == []
        assert manager.active_count == 1

    @pytest.mark.asyncio
    async def test_reuse_existing_session(self, manager):
        s1 = await manager.get_or_create("test-1")
        s2 = await manager.get_or_create("test-1")
        assert s1 is s2
        assert manager.active_count == 1

    @pytest.mark.asyncio
    async def test_add_turn(self, manager):
        await manager.get_or_create("test-1")
        await manager.add_turn("test-1", "user", "hello")
        await manager.add_turn("test-1", "assistant", "hi there")
        buf = manager.get_buffer("test-1")
        assert len(buf) == 2
        assert buf[0] == {"role": "user", "content": "hello"}
        assert buf[1] == {"role": "assistant", "content": "hi there"}

    @pytest.mark.asyncio
    async def test_turn_count_increments_on_user_only(self, manager):
        await manager.get_or_create("test-1")
        await manager.add_turn("test-1", "user", "q1")
        await manager.add_turn("test-1", "assistant", "a1")
        await manager.add_turn("test-1", "user", "q2")
        session = await manager.get_or_create("test-1")
        assert session.turn_count == 2

    @pytest.mark.asyncio
    async def test_session_expiry(self, manager):
        """Session expires after sustain_seconds of inactivity."""
        await manager.get_or_create("test-1")
        assert manager.active_count == 1
        # Wait for expiry (sustain_seconds=2)
        await asyncio.sleep(2.5)
        assert manager.active_count == 0

    @pytest.mark.asyncio
    async def test_max_turns_expires_session(self, manager):
        """Session with max_turns=5 expires after 5 user turns."""
        await manager.get_or_create("test-1")
        for i in range(5):
            await manager.add_turn("test-1", "user", f"msg-{i}")
        # Next get_or_create should create a new session
        session = await manager.get_or_create("test-1")
        assert session.turn_count == 0  # Fresh session
        assert session.buffer == []

    @pytest.mark.asyncio
    async def test_buffer_ring_limit(self, manager):
        """Buffer is capped at _MAX_BUFFER_MESSAGES."""
        await manager.get_or_create("test-1")
        for i in range(25):
            await manager.add_turn("test-1", "user", f"msg-{i}")
        buf = manager.get_buffer("test-1")
        assert len(buf) == 20  # _MAX_BUFFER_MESSAGES

    @pytest.mark.asyncio
    async def test_get_buffer_nonexistent(self, manager):
        assert manager.get_buffer("nope") == []


# ── Conversation Handler ─────────────────────────────────────────────


class TestVoiceConversationHandler:
    @pytest.fixture
    def mock_retriever(self):
        retriever = AsyncMock()
        # Return mock results with content attribute
        result = MagicMock()
        result.content = "User discussed voice interface yesterday"
        retriever.recall.return_value = [result]
        return retriever

    @pytest.fixture
    def mock_router(self):
        router = MagicMock()
        result = MagicMock()
        result.success = True
        result.content = "We talked about building a voice interface for Genesis."
        router.route_call = AsyncMock(return_value=result)
        return router

    @pytest.fixture
    def handler(self, mock_retriever, mock_router):
        return VoiceConversationHandler(
            retriever=mock_retriever,
            router=mock_router,
        )

    @pytest.mark.asyncio
    async def test_handle_basic(self, handler, mock_router):
        response = await handler.handle("what did we discuss yesterday", "sess-1")
        assert "voice interface" in response
        mock_router.route_call.assert_called_once()
        call_args = mock_router.route_call.call_args
        assert call_args.kwargs["call_site_id"] == "voice_conversation"

    @pytest.mark.asyncio
    async def test_handle_empty_transcript(self, handler):
        response = await handler.handle("", "sess-1")
        assert "didn't catch" in response

    @pytest.mark.asyncio
    async def test_handle_includes_memory_in_messages(self, handler, mock_router):
        await handler.handle("tell me about ego", "sess-1")
        messages = mock_router.route_call.call_args.kwargs["messages"]
        system_msg = messages[0]
        assert system_msg["role"] == "system"
        assert "voice interface" in system_msg["content"]  # From mock recall

    @pytest.mark.asyncio
    async def test_handle_router_failure(self, handler, mock_router):
        mock_router.route_call.return_value.success = False
        mock_router.route_call.return_value.error = "No providers"
        response = await handler.handle("hello", "sess-1")
        assert "trouble" in response

    @pytest.mark.asyncio
    async def test_handle_memory_failure_graceful(self, handler, mock_retriever, mock_router):
        mock_retriever.recall.side_effect = RuntimeError("Qdrant down")
        response = await handler.handle("hello", "sess-1")
        # Should still work — memory is best-effort
        assert response  # Non-empty response from router

    @pytest.mark.asyncio
    async def test_handle_stores_turns_in_buffer(self, handler):
        await handler.handle("first question", "sess-1")
        buf = handler.session_manager.get_buffer("sess-1")
        assert len(buf) == 2
        assert buf[0]["role"] == "user"
        assert buf[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_handle_sends_buffer_as_context(self, handler, mock_router):
        await handler.handle("first question", "sess-1")
        mock_router.route_call.reset_mock()
        await handler.handle("follow up", "sess-1")
        messages = mock_router.route_call.call_args.kwargs["messages"]
        # system + 2 buffer messages (from first turn) + new user message
        assert len(messages) == 4
        assert messages[1]["content"] == "first question"
        assert messages[3]["content"] == "follow up"


# ── Channel Adapter ──────────────────────────────────────────────────


class TestVoiceChannelAdapter:
    def test_capabilities(self):
        adapter = VoiceChannelAdapter()
        caps = adapter.get_capabilities()
        assert caps["voice"] is True
        assert caps["markdown"] is False
        assert caps["buttons"] is False

    @pytest.mark.asyncio
    async def test_send_typing_noop(self):
        adapter = VoiceChannelAdapter()
        await adapter.send_typing("ch-1")  # Should not raise

    @pytest.mark.asyncio
    async def test_engagement_signals_neutral(self):
        adapter = VoiceChannelAdapter()
        signals = await adapter.get_engagement_signals("del-1")
        assert signals["signal"] == "neutral"

    @pytest.mark.asyncio
    async def test_send_message_no_ha(self):
        """Without HA config, send_message returns empty delivery ID."""
        adapter = VoiceChannelAdapter()
        result = await adapter.send_message("ch-1", "hello")
        assert result == ""

    @pytest.mark.asyncio
    async def test_send_message_announce(self):
        """Default path uses assist_satellite.announce with preannounce chime."""
        adapter = VoiceChannelAdapter(
            ha_url="http://ha.local:8123",
            ha_token="test-token",
        )
        with patch("genesis.channels.voice.adapter.httpx.AsyncClient") as mock_client:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_post = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=mock_post),
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await adapter.send_message("ch-1", "hello world")
            assert result  # Non-empty delivery ID

            # Verify it called assist_satellite/announce, not tts/speak
            call_args = mock_post.call_args
            assert "assist_satellite/announce" in call_args[0][0]
            payload = call_args[1]["json"]
            assert payload["message"] == "hello world"
            assert payload["preannounce"] is True

    @pytest.mark.asyncio
    async def test_send_message_no_preannounce(self):
        """preannounce=False skips the chime."""
        adapter = VoiceChannelAdapter(
            ha_url="http://ha.local:8123",
            ha_token="test-token",
        )
        with patch("genesis.channels.voice.adapter.httpx.AsyncClient") as mock_client:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_post = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=mock_post),
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            await adapter.send_message("ch-1", "quick follow-up", preannounce=False)

            payload = mock_post.call_args[1]["json"]
            assert payload["preannounce"] is False

    @pytest.mark.asyncio
    async def test_send_message_fallback_to_tts(self):
        """When announce fails, falls back to tts.speak."""
        adapter = VoiceChannelAdapter(
            ha_url="http://ha.local:8123",
            ha_token="test-token",
        )
        call_count = 0

        async def mock_post_fn(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if "assist_satellite" in url:
                raise httpx.HTTPStatusError(
                    "Not Found", request=MagicMock(), response=MagicMock(status_code=404),
                )
            # tts.speak succeeds
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with patch("genesis.channels.voice.adapter.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=AsyncMock(side_effect=mock_post_fn)),
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await adapter.send_message("ch-1", "test fallback")
            assert result  # Still returns delivery ID
            assert call_count == 2  # announce attempt + tts fallback

    @pytest.mark.asyncio
    async def test_send_message_no_satellite_uses_tts(self):
        """Without satellite_entity, uses tts.speak directly."""
        adapter = VoiceChannelAdapter(
            ha_url="http://ha.local:8123",
            ha_token="test-token",
            satellite_entity="",
        )
        with patch("genesis.channels.voice.adapter.httpx.AsyncClient") as mock_client:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_post = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=mock_post),
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            await adapter.send_message("ch-1", "direct tts")

            call_args = mock_post.call_args
            assert "tts/speak" in call_args[0][0]


# ── Voice API Endpoint ───────────────────────────────────────────────


class TestVoiceAPI:
    @pytest.fixture
    def app(self):
        """Create a minimal Flask app with voice API blueprint."""
        from flask import Flask

        from genesis.dashboard.routes.voice_api import voice_api_bp

        app = Flask(__name__)
        app.register_blueprint(voice_api_bp)

        # Mock handler
        handler = AsyncMock()
        handler.handle.return_value = "I recall we discussed voice features."
        app.config["VOICE_HANDLER"] = handler

        # Mock event loop
        loop = asyncio.new_event_loop()
        app.config["GENESIS_EVENT_LOOP"] = loop

        yield app
        loop.close()

    def test_no_auth_header(self, app):
        with (
            patch.dict("os.environ", {"GENESIS_MCP_HTTP_TOKEN": "secret"}),
            app.test_client() as client,
        ):
            resp = client.post("/v1/voice/chat/completions", json={
                "messages": [{"role": "user", "content": "hello"}],
            })
            assert resp.status_code == 401

    def test_wrong_token(self, app):
        with (
            patch.dict("os.environ", {"GENESIS_MCP_HTTP_TOKEN": "secret"}),
            app.test_client() as client,
        ):
            resp = client.post(
                "/v1/voice/chat/completions",
                json={"messages": [{"role": "user", "content": "hello"}]},
                headers={"Authorization": "Bearer wrong"},
            )
            assert resp.status_code == 401

    def test_no_user_message(self, app):
        import threading

        loop = app.config["GENESIS_EVENT_LOOP"]

        def run_loop():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=run_loop, daemon=True)
        t.start()
        try:
            with (
                patch.dict("os.environ", {"GENESIS_MCP_HTTP_TOKEN": ""}, clear=False),
                app.test_client() as client,
            ):
                resp = client.post(
                    "/v1/voice/chat/completions",
                    json={"messages": [{"role": "system", "content": "you are helpful"}]},
                )
                assert resp.status_code == 400
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)

    def test_handler_not_initialized(self, app):
        app.config["VOICE_HANDLER"] = None
        with (
            patch.dict("os.environ", {"GENESIS_MCP_HTTP_TOKEN": ""}, clear=False),
            app.test_client() as client,
        ):
            resp = client.post(
                "/v1/voice/chat/completions",
                json={"messages": [{"role": "user", "content": "hello"}]},
            )
            assert resp.status_code == 503

    def test_successful_response_format(self, app):
        """Verify OpenAI-compatible response structure."""
        with patch.dict("os.environ", {"GENESIS_MCP_HTTP_TOKEN": ""}, clear=False):
            loop = app.config["GENESIS_EVENT_LOOP"]

            # Override to use synchronous mock
            import threading

            def run_loop():
                asyncio.set_event_loop(loop)
                loop.run_forever()

            t = threading.Thread(target=run_loop, daemon=True)
            t.start()

            try:
                with app.test_client() as client:
                    resp = client.post(
                        "/v1/voice/chat/completions",
                        json={
                            "messages": [{"role": "user", "content": "what happened yesterday"}],
                            "user": "ha-voice-test",
                        },
                    )
                    assert resp.status_code == 200
                    data = resp.get_json()
                    assert data["object"] == "chat.completion"
                    assert data["model"] == "genesis-voice"
                    assert len(data["choices"]) == 1
                    assert data["choices"][0]["message"]["role"] == "assistant"
                    assert data["choices"][0]["finish_reason"] == "stop"
            finally:
                loop.call_soon_threadsafe(loop.stop)
                t.join(timeout=2)
