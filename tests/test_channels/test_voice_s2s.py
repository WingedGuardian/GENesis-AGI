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
        assert len(TOOL_DECLARATIONS) == 3
        names = {t["name"] for t in TOOL_DECLARATIONS}
        assert names == {"ask_genesis", "web_search", "approve_pending"}

    def test_system_prompt_has_placeholders(self):
        assert "{voice_context}" in SYSTEM_INSTRUCTIONS

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

    async def test_ask_genesis_no_handler(self):
        bridge = GenesisBridge()
        result = await bridge.handle_tool_call(
            "ask_genesis", json.dumps({"query": "test"}),
        )
        data = json.loads(result)
        assert "answer" in data  # Returns "handler not available"

    async def test_ask_genesis_uses_raw_snippets(self):
        handler = AsyncMock()
        handler.handle = AsyncMock(return_value="Recalled memories:\n- voice Phase 1")

        bridge = GenesisBridge(voice_handler=handler)
        result = await bridge.handle_tool_call(
            "ask_genesis", json.dumps({"query": "what did we do yesterday?"}),
        )
        data = json.loads(result)
        assert "answer" in data
        assert "voice Phase 1" in data["answer"]
        # Verify raw_snippets=True is passed
        handler.handle.assert_awaited_once_with(
            transcript="what did we do yesterday?",
            session_id="s2s-s2s-default",
            raw_snippets=True,
        )

    async def test_ask_genesis_passes_satellite_id(self):
        handler = AsyncMock()
        handler.handle = AsyncMock(return_value="memories here")

        bridge = GenesisBridge(voice_handler=handler)
        result = await bridge.handle_tool_call(
            "ask_genesis",
            json.dumps({"query": "test"}),
            satellite_id="my-satellite",
        )
        data = json.loads(result)
        assert "answer" in data
        handler.handle.assert_awaited_once_with(
            transcript="test",
            session_id="s2s-my-satellite",
            raw_snippets=True,
        )

    async def test_ask_genesis_falls_back_on_raw_failure(self):
        handler = AsyncMock()
        # First call (raw_snippets=True) fails, second (full path) succeeds
        handler.handle = AsyncMock(
            side_effect=[Exception("recall failed"), "Full LLM answer"],
        )

        bridge = GenesisBridge(voice_handler=handler)
        result = await bridge.handle_tool_call(
            "ask_genesis", json.dumps({"query": "test"}),
        )
        data = json.loads(result)
        assert "answer" in data
        assert data["answer"] == "Full LLM answer"
        assert handler.handle.await_count == 2

    async def test_web_search_import_failure(self):
        bridge = GenesisBridge()
        result = await bridge.handle_tool_call(
            "web_search", json.dumps({"query": "weather"}),
        )
        data = json.loads(result)
        assert isinstance(data, dict)

    def test_get_system_prompt(self):
        bridge = GenesisBridge()
        prompt = bridge.get_system_prompt()
        assert "Genesis" in prompt
        assert "ask_genesis" in prompt
        assert "approve_pending" in prompt or "APPROVAL" in prompt

    async def test_approve_pending_no_gate(self):
        bridge = GenesisBridge()
        result = await bridge.handle_tool_call(
            "approve_pending", json.dumps({"decision": "approved"}),
        )
        data = json.loads(result)
        assert "error" in data
        assert "not available" in data["error"]

    async def test_approve_pending_success(self):
        gate = AsyncMock()
        gate.resolve_most_recent_pending_voice = AsyncMock(return_value="abc12345")

        bridge = GenesisBridge(approval_gate=gate)
        result = await bridge.handle_tool_call(
            "approve_pending", json.dumps({"decision": "approved"}),
        )
        data = json.loads(result)
        assert "result" in data
        assert "approved" in data["result"]
        gate.resolve_most_recent_pending_voice.assert_awaited_once_with(
            decision="approved", resolved_by="voice:s2s",
        )

    async def test_approve_pending_no_pending(self):
        gate = AsyncMock()
        gate.resolve_most_recent_pending_voice = AsyncMock(return_value=None)

        bridge = GenesisBridge(approval_gate=gate)
        result = await bridge.handle_tool_call(
            "approve_pending", json.dumps({"decision": "rejected"}),
        )
        data = json.loads(result)
        assert "error" in data
        assert "No pending" in data["error"]

    async def test_approve_pending_invalid_decision(self):
        gate = AsyncMock()
        bridge = GenesisBridge(approval_gate=gate)
        result = await bridge.handle_tool_call(
            "approve_pending", json.dumps({"decision": "maybe"}),
        )
        data = json.loads(result)
        assert "error" in data
        assert "Invalid" in data["error"]


# ─── VoiceConversationHandler tests ─────────────────────────────────────


class TestVoiceConversationHandler:
    async def test_raw_snippets_returns_memories(self):
        from genesis.channels.voice.handler import VoiceConversationHandler

        retriever = AsyncMock()
        result_obj = MagicMock()
        result_obj.content = "Yesterday we worked on voice pipeline."
        retriever.recall = AsyncMock(return_value=[result_obj])

        router = AsyncMock()
        handler = VoiceConversationHandler(retriever=retriever, router=router)

        response = await handler.handle("what did we do?", "test-session", raw_snippets=True)
        assert "voice pipeline" in response
        # Router should NOT be called in raw_snippets mode
        router.route_call.assert_not_awaited()

    async def test_raw_snippets_empty_recall(self):
        from genesis.channels.voice.handler import VoiceConversationHandler

        retriever = AsyncMock()
        retriever.recall = AsyncMock(return_value=[])

        router = AsyncMock()
        handler = VoiceConversationHandler(retriever=retriever, router=router)

        response = await handler.handle("what did we do?", "test-session", raw_snippets=True)
        assert "No relevant memories" in response
        router.route_call.assert_not_awaited()

    async def test_raw_snippets_recall_failure(self):
        from genesis.channels.voice.handler import VoiceConversationHandler

        retriever = AsyncMock()
        retriever.recall = AsyncMock(side_effect=Exception("Qdrant down"))

        router = AsyncMock()
        handler = VoiceConversationHandler(retriever=retriever, router=router)

        response = await handler.handle("test", "test-session", raw_snippets=True)
        assert "No relevant memories" in response

    async def test_empty_transcript(self):
        from genesis.channels.voice.handler import VoiceConversationHandler

        retriever = AsyncMock()
        router = AsyncMock()
        handler = VoiceConversationHandler(retriever=retriever, router=router)

        response = await handler.handle("   ", "test-session", raw_snippets=True)
        assert "didn't catch that" in response


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

    async def test_close_stores_transcript(self):
        from genesis.channels.voice.s2s_session import S2SSessionManager

        store = AsyncMock()
        store.store = AsyncMock(return_value="mem-123")

        bridge = GenesisBridge()
        mgr = S2SSessionManager(bridge=bridge, memory_store=store)
        session = await mgr.get_or_create("sat-1")
        session.input_transcript = "What did we work on?"
        session.output_transcript = "We worked on voice pipeline."

        await mgr.close("sat-1")

        store.store.assert_awaited_once()
        call_kwargs = store.store.call_args
        content = call_kwargs[0][0]  # first positional arg
        assert "What did we work on?" in content
        assert "We worked on voice pipeline." in content
        assert call_kwargs[1]["source"] == "voice_s2s"
        assert call_kwargs[1]["wing"] == "channels"
        assert call_kwargs[1]["room"] == "voice"

    async def test_close_skips_store_when_empty(self):
        from genesis.channels.voice.s2s_session import S2SSessionManager

        store = AsyncMock()
        store.store = AsyncMock()

        bridge = GenesisBridge()
        mgr = S2SSessionManager(bridge=bridge, memory_store=store)
        await mgr.get_or_create("sat-1")
        # No transcripts set

        await mgr.close("sat-1")
        store.store.assert_not_awaited()

    async def test_close_handles_store_failure(self):
        from genesis.channels.voice.s2s_session import S2SSessionManager

        store = AsyncMock()
        store.store = AsyncMock(side_effect=Exception("DB error"))

        bridge = GenesisBridge()
        mgr = S2SSessionManager(bridge=bridge, memory_store=store)
        session = await mgr.get_or_create("sat-1")
        session.input_transcript = "hello"
        session.output_transcript = "hi"

        # Should not raise — store failure is best-effort
        inp, out = await mgr.close("sat-1")
        assert inp == "hello"
        assert out == "hi"


# ─── Voice hours + _should_voice tests ─────────────────────────────────


class TestVoiceHours:
    """Test _in_voice_hours midnight-wrap logic and _should_voice filtering."""

    def _make_pipeline(self, *, voice_hours=(9, 2), has_voice=True):
        """Build a minimal pipeline with voice config for testing."""
        from genesis.outreach.config import OutreachConfig, QuietHours
        from genesis.outreach.pipeline import OutreachPipeline

        config = OutreachConfig(
            quiet_hours=QuietHours(start="22:00", end="07:00"),
            channel_preferences={"default": "telegram"},
            thresholds={},
            max_daily=5,
            surplus_daily=1,
            content_daily=3,
            notification_daily=10,
            morning_report_time="07:00",
            engagement_timeout_hours=24,
            engagement_poll_minutes=60,
            voice_hours=voice_hours,
        )
        channels = {}
        if has_voice:
            channels["voice"] = MagicMock()
        pipe = OutreachPipeline(
            governance=MagicMock(),
            drafter=MagicMock(),
            formatter=MagicMock(),
            channels=channels,
            config=config,
        )
        return pipe

    def _check_hour(self, pipe, hour, expected):
        """Helper: check _in_voice_hours at a given hour."""
        from datetime import UTC
        from datetime import datetime as real_dt

        fake_now = real_dt(2026, 6, 7, hour, 30, tzinfo=UTC)
        with (
            patch("genesis.env.user_timezone", return_value="UTC"),
            patch("genesis.outreach.pipeline.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fake_now
            assert pipe._in_voice_hours() is expected, f"hour={hour}"

    def test_voice_hours_10pm_within_wrap(self):
        """10pm should be within (9, 2) = 9am-2am."""
        self._check_hour(self._make_pipeline(voice_hours=(9, 2)), 22, True)

    def test_voice_hours_1am_within_wrap(self):
        """1am should be within (9, 2) = 9am-2am."""
        self._check_hour(self._make_pipeline(voice_hours=(9, 2)), 1, True)

    def test_voice_hours_3am_outside_wrap(self):
        """3am should be outside (9, 2) = 9am-2am."""
        self._check_hour(self._make_pipeline(voice_hours=(9, 2)), 3, False)

    def test_voice_hours_9am_boundary(self):
        """9am should be within (9, 2)."""
        self._check_hour(self._make_pipeline(voice_hours=(9, 2)), 9, True)

    def test_voice_hours_2am_boundary_excluded(self):
        """2am should be outside (9, 2) — end is exclusive."""
        self._check_hour(self._make_pipeline(voice_hours=(9, 2)), 2, False)

    def test_should_voice_no_voice_channel(self):
        """_should_voice returns False when no voice channel registered."""
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        pipe = self._make_pipeline(has_voice=False)
        req = OutreachRequest(
            category=OutreachCategory.ALERT,
            topic="test", context="test", salience_score=1.0,
        )
        assert not pipe._should_voice(req)

    def test_should_voice_wrong_category(self):
        """_should_voice returns False for non-alert categories."""
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        pipe = self._make_pipeline()
        req = OutreachRequest(
            category=OutreachCategory.SURPLUS,
            topic="test", context="test", salience_score=1.0,
        )
        with patch.object(pipe, "_in_voice_hours", return_value=True):
            assert not pipe._should_voice(req)

    def test_should_voice_alert_in_hours(self):
        """_should_voice returns True for ALERT category during voice hours."""
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        pipe = self._make_pipeline()
        req = OutreachRequest(
            category=OutreachCategory.ALERT,
            topic="test", context="test", salience_score=1.0,
        )
        with patch.object(pipe, "_in_voice_hours", return_value=True):
            assert pipe._should_voice(req)

    def test_should_voice_blocker_in_hours(self):
        """_should_voice returns True for BLOCKER category during voice hours."""
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        pipe = self._make_pipeline()
        req = OutreachRequest(
            category=OutreachCategory.BLOCKER,
            topic="test", context="test", salience_score=1.0,
        )
        with patch.object(pipe, "_in_voice_hours", return_value=True):
            assert pipe._should_voice(req)
