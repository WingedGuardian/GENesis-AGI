"""Tests for voice channel — sessions, handler, adapter, and API endpoint."""

from __future__ import annotations

import asyncio
import os
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

    def test_entity_defaults_from_env(self):
        """Entity IDs read from env vars when not passed explicitly."""
        with patch.dict("os.environ", {
            "HA_TTS_ENTITY": "tts.custom",
            "HA_MEDIA_PLAYER_ENTITY": "media_player.custom_device",
        }):
            adapter = VoiceChannelAdapter()
            assert adapter._tts_entity == "tts.custom"
            assert adapter._media_player == "media_player.custom_device"

    def test_entity_explicit_overrides_env(self):
        """Explicit constructor params take precedence over env vars."""
        with patch.dict("os.environ", {
            "HA_TTS_ENTITY": "tts.from_env",
            "HA_MEDIA_PLAYER_ENTITY": "media_player.from_env",
        }):
            adapter = VoiceChannelAdapter(
                tts_entity="tts.explicit",
                media_player_entity="media_player.explicit",
            )
            assert adapter._tts_entity == "tts.explicit"
            assert adapter._media_player == "media_player.explicit"

    def test_media_player_empty_without_config(self):
        """No device-specific default: without env/param, media_player is empty
        (proactive voice disabled) — a hardcoded device id silently broke when
        the device was renamed. tts keeps the generic piper default."""
        # Scrub any HA_ env vars that might be set in the test environment
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("HA_")}
        with patch.dict("os.environ", env, clear=True):
            adapter = VoiceChannelAdapter()
            assert adapter._tts_entity == "tts.piper"
            assert adapter._media_player == ""

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
    async def test_send_message_with_chime(self):
        """Default path: chime via media_player.play_media, then tts.speak."""
        adapter = VoiceChannelAdapter(
            ha_url="http://ha.local:8123",
            ha_token="test-token",
            media_player_entity="media_player.test",
        )
        payloads = []

        async def mock_post_fn(url, **kwargs):
            payloads.append((url, kwargs.get("json", {})))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with (
            patch("genesis.channels.voice.adapter.httpx.AsyncClient") as mock_client,
            patch("genesis.channels.voice.adapter.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=AsyncMock(side_effect=mock_post_fn)),
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await adapter.send_message("ch-1", "hello world")
            assert result  # Non-empty delivery ID

            # Two calls: play_media (chime) then tts.speak
            assert len(payloads) == 2
            chime_url, chime_payload = payloads[0]
            assert "media_player/play_media" in chime_url
            assert chime_payload["media_content_id"] == adapter.DEFAULT_CHIME_MEDIA_ID
            assert chime_payload["announce"] is True
            assert "tts/speak" in payloads[1][0]
            # No assist_satellite calls
            assert not any("assist_satellite" in p[0] for p in payloads)

    @pytest.mark.asyncio
    async def test_send_message_no_preannounce(self):
        """preannounce=False skips the chime, only calls tts.speak."""
        adapter = VoiceChannelAdapter(
            ha_url="http://ha.local:8123",
            ha_token="test-token",
            media_player_entity="media_player.test",
        )
        calls = []

        async def mock_post_fn(url, **kwargs):
            calls.append(url)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with patch("genesis.channels.voice.adapter.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=AsyncMock(side_effect=mock_post_fn)),
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            await adapter.send_message("ch-1", "quick follow-up", preannounce=False)

            # Only tts.speak, no announce
            assert len(calls) == 1
            assert "tts/speak" in calls[0]

    @pytest.mark.asyncio
    async def test_send_message_chime_failure_still_speaks(self):
        """When chime fails, TTS still plays."""
        adapter = VoiceChannelAdapter(
            ha_url="http://ha.local:8123",
            ha_token="test-token",
            media_player_entity="media_player.test",
        )
        calls = []

        async def mock_post_fn(url, **kwargs):
            calls.append(url)
            if "media_player/play_media" in url:
                raise httpx.HTTPStatusError(
                    "Not Found", request=MagicMock(), response=MagicMock(status_code=404),
                )
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
            # Chime failed but tts.speak still called
            assert any("tts/speak" in c for c in calls)

    @pytest.mark.asyncio
    async def test_send_message_no_preannounce_skips_chime(self):
        """preannounce=False skips chime even with chime configured."""
        adapter = VoiceChannelAdapter(
            ha_url="http://ha.local:8123",
            ha_token="test-token",
            media_player_entity="media_player.test",
        )
        calls = []

        async def mock_post_fn(url, **kwargs):
            calls.append(url)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with patch("genesis.channels.voice.adapter.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=AsyncMock(side_effect=mock_post_fn)),
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            await adapter.send_message("ch-1", "direct tts", preannounce=False)

            assert len(calls) == 1
            assert "tts/speak" in calls[0]

    @pytest.mark.asyncio
    async def test_send_message_no_media_player_skips(self):
        """No media_player entity configured → send_message skips delivery
        entirely (no HA calls), instead of POSTing to a nonexistent entity."""
        adapter = VoiceChannelAdapter(
            ha_url="http://ha.local:8123",
            ha_token="test-token",
            media_player_entity="",
        )
        posted = []

        async def mock_post_fn(url, **kwargs):
            posted.append(url)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with patch("genesis.channels.voice.adapter.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=AsyncMock(side_effect=mock_post_fn)),
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await adapter.send_message("ch-1", "hello")
            assert result == ""
            assert posted == []

    @pytest.mark.asyncio
    async def test_tts_empty_changed_states_warns(self, caplog):
        """tts.speak returning [] (no entity matched → no audio) logs a WARNING
        rather than a false 'delivered'. Guards the silent-chime failure mode
        (HA 200 + empty changed-states) from 2026-06-13."""
        import logging

        adapter = VoiceChannelAdapter(
            ha_url="http://ha.local:8123",
            ha_token="test-token",
            media_player_entity="media_player.bogus",
        )

        async def mock_post_fn(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value=[])  # HA: zero states changed
            return resp

        with (
            patch("genesis.channels.voice.adapter.httpx.AsyncClient") as mock_client,
            patch("genesis.channels.voice.adapter.asyncio.sleep", new_callable=AsyncMock),
            caplog.at_level(logging.WARNING, logger="genesis.channels.voice.adapter"),
        ):
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=AsyncMock(side_effect=mock_post_fn)),
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await adapter.send_message("ch-1", "hello")
            assert result  # still returns a delivery id (best-effort)
            assert any("changed no states" in r.getMessage() for r in caplog.records)


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

    # ── S2S Bridge Tool Dispatch Endpoints ──────────────────────────

    def test_tool_call_no_bridge(self, app):
        """Returns 503 when bridge is not initialized."""
        with (
            patch.dict("os.environ", {"GENESIS_MCP_HTTP_TOKEN": ""}, clear=False),
            app.test_client() as client,
        ):
            resp = client.post(
                "/v1/voice/tool_call",
                json={"tool_name": "ask_genesis", "arguments": {"query": "test"}},
            )
            assert resp.status_code == 503

    def test_tool_call_missing_name(self, app):
        """Returns 400 when tool_name is missing."""
        import json as json_mod
        bridge = MagicMock()
        bridge.handle_tool_call = AsyncMock(return_value=json_mod.dumps({"answer": "ok"}))
        app.config["GENESIS_BRIDGE"] = bridge

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
                    "/v1/voice/tool_call",
                    json={"arguments": {"query": "test"}},
                )
                assert resp.status_code == 400
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)

    def test_tool_call_dispatches_to_bridge(self, app):
        """Successful tool call dispatches to GenesisBridge."""
        import json as json_mod
        import threading

        bridge = MagicMock()
        bridge.handle_tool_call = AsyncMock(
            return_value=json_mod.dumps({"answer": "Genesis recalls you worked on voice."})
        )
        app.config["GENESIS_BRIDGE"] = bridge

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
                    "/v1/voice/tool_call",
                    json={"tool_name": "ask_genesis", "arguments": {"query": "what did we work on"}},
                )
                assert resp.status_code == 200
                data = resp.get_json()
                assert "answer" in data
                assert "voice" in data["answer"]
                bridge.handle_tool_call.assert_called_once()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)

    def test_tool_call_auth_required(self, app):
        """Auth check works on tool_call endpoint."""
        with (
            patch.dict("os.environ", {"GENESIS_MCP_HTTP_TOKEN": "secret"}),
            app.test_client() as client,
        ):
            resp = client.post(
                "/v1/voice/tool_call",
                json={"tool_name": "ask_genesis", "arguments": {}},
            )
            assert resp.status_code == 401

    def test_system_prompt_endpoint(self, app):
        """System prompt endpoint returns Genesis persona."""
        bridge = MagicMock()
        bridge.get_system_prompt.return_value = "You are Genesis, a cognitive AI partner."
        app.config["GENESIS_BRIDGE"] = bridge

        with (
            patch.dict("os.environ", {"GENESIS_MCP_HTTP_TOKEN": ""}, clear=False),
            app.test_client() as client,
        ):
            resp = client.get("/v1/voice/system_prompt")
            assert resp.status_code == 200
            data = resp.get_json()
            assert "prompt" in data
            assert "Genesis" in data["prompt"]

    def test_tool_declarations_endpoint(self, app):
        """Tool declarations endpoint returns the 3 Genesis voice tools."""
        with (
            patch.dict("os.environ", {"GENESIS_MCP_HTTP_TOKEN": ""}, clear=False),
            app.test_client() as client,
        ):
            resp = client.get("/v1/voice/tool_declarations")
            assert resp.status_code == 200
            data = resp.get_json()
            assert "tools" in data
            tool_names = [t["name"] for t in data["tools"]]
            assert "ask_genesis" in tool_names
            assert "web_search" in tool_names
            assert "approve_pending" in tool_names
