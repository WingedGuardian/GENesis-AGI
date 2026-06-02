"""Tests for the S2S voice pipeline components."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.channels.voice.config import (
    s2s_enabled,
    s2s_model,
    s2s_provider,
    s2s_voice,
    wyoming_stt_port,
    wyoming_tts_port,
)
from genesis.channels.voice.genesis_bridge import (
    SYSTEM_INSTRUCTIONS,
    TOOL_DECLARATIONS,
    GenesisBridge,
)


# ─── Config tests ────────────────────────────────────────────────────────


class TestVoiceConfig:
    def test_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            assert s2s_provider() == "openai"
            assert s2s_model() == "gpt-realtime-1.5"
            assert s2s_voice() == "alloy"
            assert wyoming_stt_port() == 10300
            assert wyoming_tts_port() == 10301

    def test_gemini_provider(self):
        with patch.dict("os.environ", {"VOICE_S2S_PROVIDER": "gemini"}):
            assert s2s_provider() == "gemini"
            assert s2s_model() == "gemini-2.0-flash-live"

    def test_custom_model(self):
        with patch.dict("os.environ", {"VOICE_S2S_MODEL": "gpt-realtime-2"}):
            assert s2s_model() == "gpt-realtime-2"

    def test_s2s_enabled_openai(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test"}):
            assert s2s_enabled()

    def test_s2s_disabled_no_key(self):
        with patch.dict("os.environ", {}, clear=True):
            assert not s2s_enabled()

    def test_s2s_enabled_gemini(self):
        with patch.dict("os.environ", {
            "VOICE_S2S_PROVIDER": "gemini",
            "GOOGLE_API_KEY": "test",
        }):
            assert s2s_enabled()


# ─── GenesisBridge tests ─────────────────────────────────────────────────


class TestGenesisBridge:
    def test_tool_declarations_structure(self):
        assert len(TOOL_DECLARATIONS) == 2
        names = {t["name"] for t in TOOL_DECLARATIONS}
        assert names == {"ask_genesis", "web_search"}

    def test_system_prompt_has_placeholders(self):
        assert "{essential_knowledge}" in SYSTEM_INSTRUCTIONS

    async def test_handle_unknown_tool(self):
        bridge = GenesisBridge()
        result = await bridge.handle_tool_call("unknown_tool", '{"x": 1}')
        data = json.loads(result)
        assert "error" in data
        assert "Unknown tool" in data["error"]

    async def test_handle_invalid_args(self):
        bridge = GenesisBridge()
        result = await bridge.handle_tool_call("ask_genesis", "not json{{{")
        data = json.loads(result)
        assert "error" in data

    async def test_ask_genesis_no_retriever(self):
        bridge = GenesisBridge()
        result = await bridge.handle_tool_call(
            "ask_genesis", json.dumps({"query": "test"}),
        )
        data = json.loads(result)
        # Without retriever, should still return a structured response
        assert "answer" in data or "essential_knowledge" in data

    async def test_ask_genesis_with_retriever(self):
        retriever = AsyncMock()
        retriever.recall = AsyncMock(return_value=[
            {"content": "Yesterday we worked on voice interface Phase 1."},
        ])

        bridge = GenesisBridge(retriever=retriever)
        result = await bridge.handle_tool_call(
            "ask_genesis", json.dumps({"query": "what did we do yesterday?"}),
        )
        data = json.loads(result)
        assert "memories" in data
        retriever.recall.assert_awaited_once()

    async def test_web_search_import_failure(self):
        bridge = GenesisBridge()
        # web_search will fail on import — should return error gracefully
        result = await bridge.handle_tool_call(
            "web_search", json.dumps({"query": "weather"}),
        )
        data = json.loads(result)
        # Should handle gracefully (either results or error)
        assert isinstance(data, dict)

    def test_get_system_prompt(self):
        bridge = GenesisBridge()
        prompt = bridge.get_system_prompt()
        assert "Genesis" in prompt
        assert "ask_genesis" in prompt


# ─── Wyoming TTS server tests ───────────────────────────────────────────


class TestWyomingTTSServer:
    def test_queue_audio(self):
        from genesis.channels.voice.wyoming_tts import WyomingTTSServer

        server = WyomingTTSServer()
        server.queue_audio(b"\x00\x00" * 1000)
        assert len(server._audio_queue) == 1

    def test_queue_max_size(self):
        from genesis.channels.voice.wyoming_tts import WyomingTTSServer

        server = WyomingTTSServer()
        for i in range(10):
            server.queue_audio(bytes([i]) * 100)
        # maxlen=5
        assert len(server._audio_queue) == 5


# ─── S2S Session Manager tests ──────────────────────────────────────────


class TestS2SSessionManager:
    @pytest.fixture(autouse=True)
    def _mock_openai_key(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            yield

    async def test_create_session(self):
        from genesis.channels.voice.s2s_session import S2SSessionManager

        bridge = GenesisBridge()
        mgr = S2SSessionManager(bridge=bridge)
        session = await mgr.get_or_create("test-satellite")
        assert session.satellite_id == "test-satellite"
        assert session.connection is None  # Not connected yet
        assert session.turn_count == 0

    async def test_reuse_session(self):
        from genesis.channels.voice.s2s_session import S2SSessionManager

        bridge = GenesisBridge()
        mgr = S2SSessionManager(bridge=bridge)
        s1 = await mgr.get_or_create("sat-1")
        # Simulate connection so it's reused
        s1.connection = MagicMock()
        s2 = await mgr.get_or_create("sat-1")
        assert s1 is s2

    async def test_close_session(self):
        from genesis.channels.voice.s2s_session import S2SSessionManager

        bridge = GenesisBridge()
        mgr = S2SSessionManager(bridge=bridge)
        session = await mgr.get_or_create("sat-1")
        session.input_transcript = "hello"
        session.output_transcript = "hi there"

        inp, out = await mgr.close("sat-1")
        assert inp == "hello"
        assert out == "hi there"
        assert "sat-1" not in mgr._sessions
