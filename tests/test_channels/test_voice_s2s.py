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
        with patch.dict(
            "os.environ",
            {
                "VOICE_S2S_PROVIDER": "gemini",
                "GOOGLE_API_KEY": "test",
            },
        ):
            assert s2s_enabled()


# ─── GenesisBridge tests ─────────────────────────────────────────────────


class TestGenesisBridge:
    def test_tool_declarations_structure(self):
        # ask_genesis is disabled until the voice-memory refactor. Only
        # web_search + approve_pending are advertised; the dispatch and
        # _ask_genesis implementation stay in place for easy re-enable.
        assert len(TOOL_DECLARATIONS) == 2
        names = {t["name"] for t in TOOL_DECLARATIONS}
        assert names == {"web_search", "approve_pending"}
        assert "ask_genesis" not in names

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
            "ask_genesis",
            json.dumps({"query": "test"}),
        )
        data = json.loads(result)
        assert "answer" in data  # Returns "handler not available"

    async def test_ask_genesis_uses_raw_snippets(self):
        handler = AsyncMock()
        handler.handle = AsyncMock(return_value="Recalled memories:\n- voice Phase 1")

        bridge = GenesisBridge(voice_handler=handler)
        result = await bridge.handle_tool_call(
            "ask_genesis",
            json.dumps({"query": "what did we do yesterday?"}),
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
            "ask_genesis",
            json.dumps({"query": "test"}),
        )
        data = json.loads(result)
        assert "answer" in data
        assert data["answer"] == "Full LLM answer"
        assert handler.handle.await_count == 2

    async def test_web_search_import_failure(self):
        bridge = GenesisBridge()
        result = await bridge.handle_tool_call(
            "web_search",
            json.dumps({"query": "weather"}),
        )
        data = json.loads(result)
        assert isinstance(data, dict)

    def test_get_system_prompt(self):
        bridge = GenesisBridge()
        prompt = bridge.get_system_prompt()
        assert "Genesis" in prompt
        assert "ask_genesis" not in prompt  # disabled until the memory refactor
        assert "approve_pending" in prompt or "APPROVAL" in prompt

    async def test_approve_pending_no_gate(self):
        bridge = GenesisBridge()
        result = await bridge.handle_tool_call(
            "approve_pending",
            json.dumps({"decision": "approved"}),
        )
        data = json.loads(result)
        assert "error" in data
        assert "not available" in data["error"]

    async def test_approve_pending_success(self):
        gate = AsyncMock()
        gate.resolve_pending_voice = AsyncMock(
            return_value={
                "status": "resolved",
                "request_id": "abc12345",
                "label": "run tests",
            }
        )

        bridge = GenesisBridge(approval_gate=gate)
        result = await bridge.handle_tool_call(
            "approve_pending",
            json.dumps({"decision": "approved"}),
        )
        data = json.loads(result)
        assert "result" in data
        assert "approved" in data["result"]
        assert data["action"] == "run tests"
        gate.resolve_pending_voice.assert_awaited_once_with(
            decision="approved",
            resolved_by="voice:s2s",
            request_id=None,
        )

    async def test_approve_pending_by_id(self):
        gate = AsyncMock()
        gate.resolve_pending_voice = AsyncMock(
            return_value={
                "status": "resolved",
                "request_id": "id-2",
                "label": "send email",
            }
        )

        bridge = GenesisBridge(approval_gate=gate)
        result = await bridge.handle_tool_call(
            "approve_pending",
            json.dumps({"decision": "approved", "request_id": "id-2"}),
        )
        data = json.loads(result)
        assert "result" in data
        gate.resolve_pending_voice.assert_awaited_once_with(
            decision="approved",
            resolved_by="voice:s2s",
            request_id="id-2",
        )

    async def test_approve_pending_ambiguous_asks_which(self):
        gate = AsyncMock()
        gate.resolve_pending_voice = AsyncMock(
            return_value={
                "status": "ambiguous",
                "candidates": [
                    {"id": "id-1", "label": "run tests"},
                    {"id": "id-2", "label": "send email"},
                ],
            }
        )

        bridge = GenesisBridge(approval_gate=gate)
        result = await bridge.handle_tool_call(
            "approve_pending",
            json.dumps({"decision": "approved"}),
        )
        data = json.loads(result)
        assert "needs_clarification" in data
        assert {p["request_id"] for p in data["pending"]} == {"id-1", "id-2"}

    async def test_approve_pending_not_found(self):
        gate = AsyncMock()
        gate.resolve_pending_voice = AsyncMock(return_value={"status": "not_found"})

        bridge = GenesisBridge(approval_gate=gate)
        result = await bridge.handle_tool_call(
            "approve_pending",
            json.dumps({"decision": "approved", "request_id": "gone"}),
        )
        data = json.loads(result)
        assert "error" in data
        assert "no longer pending" in data["error"]

    async def test_approve_pending_no_pending(self):
        gate = AsyncMock()
        gate.resolve_pending_voice = AsyncMock(return_value={"status": "none"})

        bridge = GenesisBridge(approval_gate=gate)
        result = await bridge.handle_tool_call(
            "approve_pending",
            json.dumps({"decision": "rejected"}),
        )
        data = json.loads(result)
        assert "error" in data
        assert "No pending" in data["error"]

    async def test_approve_pending_invalid_decision(self):
        gate = AsyncMock()
        bridge = GenesisBridge(approval_gate=gate)
        result = await bridge.handle_tool_call(
            "approve_pending",
            json.dumps({"decision": "maybe"}),
        )
        data = json.loads(result)
        assert "error" in data
        assert "Invalid" in data["error"]

    async def test_approve_pending_gate_invalid_decision(self):
        # Defensive: if the gate reports invalid_decision, surface it clearly
        # rather than the misleading "No pending approval request found".
        gate = AsyncMock()
        gate.resolve_pending_voice = AsyncMock(
            return_value={"status": "invalid_decision"},
        )
        bridge = GenesisBridge(approval_gate=gate)
        result = await bridge._approve_pending("approved")
        data = json.loads(result)
        assert "error" in data
        assert "Invalid decision" in data["error"]

    async def test_approve_pending_e2e_real_gate(self, tmp_path):
        """E2E: bridge -> real gate -> real ApprovalManager -> real DB.

        Two pending => "approve" must NOT guess; resolving by id hits exactly
        one; then a lone pending resolves unambiguously.
        """
        from unittest.mock import MagicMock

        import aiosqlite

        from genesis.autonomy.approval import ApprovalManager
        from genesis.autonomy.approval_gate import AutonomousCliApprovalGate
        from genesis.db.schema import create_all_tables

        conn = await aiosqlite.connect(str(tmp_path / "e2e.db"))
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        try:
            mgr = ApprovalManager(db=conn)
            rid1 = await mgr.request_approval(
                action_type="sentinel_dispatch",
                action_class="reversible",
                description="investigate the disk alert",
            )
            rid2 = await mgr.request_approval(
                action_type="autonomous_cli_fallback",
                action_class="reversible",
                description="run the deploy script",
            )
            gate = AutonomousCliApprovalGate(
                runtime=MagicMock(),
                approval_manager=mgr,
            )
            bridge = GenesisBridge(approval_gate=gate)

            # Two pending -> bare "approve" refuses to guess.
            amb = json.loads(
                await bridge.handle_tool_call(
                    "approve_pending",
                    json.dumps({"decision": "approved"}),
                )
            )
            assert "needs_clarification" in amb
            assert {p["request_id"] for p in amb["pending"]} == {rid1, rid2}
            assert (await mgr.get_by_id(rid1))["status"] == "pending"
            assert (await mgr.get_by_id(rid2))["status"] == "pending"

            # Resolve the specific one by id.
            ok = json.loads(
                await bridge.handle_tool_call(
                    "approve_pending",
                    json.dumps({"decision": "approved", "request_id": rid2}),
                )
            )
            assert ok["result"] == "Request approved"
            assert ok["action"] == "run the deploy script"
            assert (await mgr.get_by_id(rid2))["status"] == "approved"
            assert (await mgr.get_by_id(rid1))["status"] == "pending"

            # Now only one pending -> unambiguous resolution.
            ok2 = json.loads(
                await bridge.handle_tool_call(
                    "approve_pending",
                    json.dumps({"decision": "rejected"}),
                )
            )
            assert ok2["result"] == "Request rejected"
            assert (await mgr.get_by_id(rid1))["status"] == "rejected"
        finally:
            await conn.close()


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

    def test_clear_queue_empties_and_resets_event(self):
        from genesis.channels.voice.wyoming_tts import WyomingTTSServer

        server = WyomingTTSServer()
        server.queue_audio(b"\x01" * 100)
        server.queue_audio(b"\x02" * 100)
        assert len(server._audio_queue) == 2
        assert server._audio_ready.is_set()

        server.clear_queue()
        assert len(server._audio_queue) == 0
        assert not server._audio_ready.is_set()

    def test_clear_queue_noop_when_empty(self):
        from genesis.channels.voice.wyoming_tts import WyomingTTSServer

        server = WyomingTTSServer()
        # Should not raise or log when queue is already empty
        server.clear_queue()
        assert len(server._audio_queue) == 0


# ─── STT handler queue + session cleanup tests ────────────────────────


class TestSTTQueueCleanup:
    """Tests for TTS queue clearing on failures and AudioStart."""

    def _make_handler(self, *, s2s_manager=None, tts_server=None):
        """Build a STTEventHandler with mocked dependencies."""
        from genesis.channels.voice.wyoming_stt import STTEventHandler

        handler = STTEventHandler(
            MagicMock(),  # reader
            MagicMock(),  # writer
            s2s_manager=s2s_manager,
            tts_server=tts_server,
        )
        return handler

    async def test_audio_start_clears_tts_queue(self):
        """AudioStart should flush stale audio from the TTS queue."""
        from wyoming.audio import AudioStart

        from genesis.channels.voice.wyoming_tts import WyomingTTSServer

        tts = WyomingTTSServer()
        tts.queue_audio(b"\x01" * 100)  # orphaned audio
        assert len(tts._audio_queue) == 1

        handler = self._make_handler(tts_server=tts)
        event = AudioStart(rate=16000, width=2, channels=1).event()
        await handler.handle_event(event)

        assert len(tts._audio_queue) == 0
        assert not tts._audio_ready.is_set()

    async def test_audio_start_noop_without_tts_server(self):
        """AudioStart should not fail when no TTS server is set."""
        from wyoming.audio import AudioStart

        handler = self._make_handler(tts_server=None)
        event = AudioStart(rate=16000, width=2, channels=1).event()
        # Should not raise
        result = await handler.handle_event(event)
        assert result is True

    async def test_s2s_error_closes_session_and_clears_queue(self):
        """S2S error event should close the dead session and clear queue."""
        from genesis.channels.voice.s2s_session import S2SResponseEvent
        from genesis.channels.voice.wyoming_tts import WyomingTTSServer

        tts = WyomingTTSServer()
        tts.queue_audio(b"\xff" * 100)  # simulate pre-existing audio
        s2s_mgr = AsyncMock()
        s2s_mgr.close = AsyncMock()

        # Simulate: get_or_create returns session, connect succeeds,
        # send_turn succeeds, but receive_response yields an error.
        mock_session = MagicMock()
        mock_session.connection = MagicMock()
        s2s_mgr.get_or_create = AsyncMock(return_value=mock_session)
        s2s_mgr.connect = AsyncMock()
        s2s_mgr.send_turn = AsyncMock()

        async def _error_response(session):
            yield S2SResponseEvent(type="error", text="WebSocket closed")

        s2s_mgr.receive_response = _error_response

        handler = self._make_handler(s2s_manager=s2s_mgr, tts_server=tts)
        handler._handle_fallback = AsyncMock(return_value="fallback text")

        # Need enough audio to pass the minimum threshold
        audio = b"\x00\x00" * 3200  # 200ms at 16kHz
        result = await handler._handle_s2s(audio)

        assert result == "fallback text"
        assert len(tts._audio_queue) == 0  # queue was cleared
        s2s_mgr.close.assert_awaited_once_with("ha-voice-default")

    async def test_s2s_exception_closes_session_and_clears_queue(self):
        """Exception in _handle_s2s should close session and clear queue."""
        from genesis.channels.voice.wyoming_tts import WyomingTTSServer

        tts = WyomingTTSServer()
        tts.queue_audio(b"\x01" * 100)  # simulate partial audio queued
        s2s_mgr = AsyncMock()
        s2s_mgr.close = AsyncMock()

        mock_session = MagicMock()
        mock_session.connection = MagicMock()
        s2s_mgr.get_or_create = AsyncMock(return_value=mock_session)
        s2s_mgr.connect = AsyncMock()
        s2s_mgr.send_turn = AsyncMock(side_effect=ConnectionError("dead WS"))

        handler = self._make_handler(s2s_manager=s2s_mgr, tts_server=tts)
        handler._handle_fallback = AsyncMock(return_value="fallback text")

        audio = b"\x00\x00" * 3200
        result = await handler._handle_s2s(audio)

        assert result == "fallback text"
        assert len(tts._audio_queue) == 0  # queue cleared
        s2s_mgr.close.assert_awaited_once_with("ha-voice-default")

    async def test_s2s_exception_close_failure_still_falls_back(self):
        """If closing the session also fails, we still fall back gracefully."""
        s2s_mgr = AsyncMock()
        s2s_mgr.close = AsyncMock(side_effect=Exception("close also broken"))

        mock_session = MagicMock()
        mock_session.connection = MagicMock()
        s2s_mgr.get_or_create = AsyncMock(return_value=mock_session)
        s2s_mgr.connect = AsyncMock()
        s2s_mgr.send_turn = AsyncMock(side_effect=ConnectionError("dead"))

        handler = self._make_handler(s2s_manager=s2s_mgr, tts_server=None)
        handler._handle_fallback = AsyncMock(return_value="fallback")

        audio = b"\x00\x00" * 3200
        result = await handler._handle_s2s(audio)

        assert result == "fallback"
        # close was attempted even though it failed
        s2s_mgr.close.assert_awaited_once()


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

    async def test_close_finalizes_transcript_session_not_a_blob_store(self):
        """W0.5: close() marks the transcript session completed — the legacy
        one-blob memory_store landing is gone (turns are written per-turn
        via _record_turn as the conversation happens)."""
        from genesis.channels.voice.s2s_session import S2SSessionManager

        writer = MagicMock()
        writer.close_session = AsyncMock()
        writer.append_message = AsyncMock()

        bridge = GenesisBridge()
        mgr = S2SSessionManager(bridge=bridge, transcript_writer=writer)
        session = await mgr.get_or_create("sat-1")
        session.input_transcript = "What did we work on?"
        session.output_transcript = "We worked on voice pipeline."

        await mgr.close("sat-1")

        writer.close_session.assert_awaited_once_with(session.session_id)
        assert not hasattr(mgr, "_memory_store")

    async def test_close_skips_finalize_when_empty(self):
        from genesis.channels.voice.s2s_session import S2SSessionManager

        writer = MagicMock()
        writer.close_session = AsyncMock()

        bridge = GenesisBridge()
        mgr = S2SSessionManager(bridge=bridge, transcript_writer=writer)
        await mgr.get_or_create("sat-1")
        # No transcripts set

        await mgr.close("sat-1")
        writer.close_session.assert_not_awaited()

    async def test_close_handles_writer_failure(self):
        from genesis.channels.voice.s2s_session import S2SSessionManager

        writer = MagicMock()
        writer.close_session = AsyncMock(side_effect=Exception("DB error"))

        bridge = GenesisBridge()
        mgr = S2SSessionManager(bridge=bridge, transcript_writer=writer)
        session = await mgr.get_or_create("sat-1")
        session.input_transcript = "hello"
        session.output_transcript = "hi"

        # Should not raise — transcript finalize is best-effort
        inp, out = await mgr.close("sat-1")
        assert inp == "hello"
        assert out == "hi"

    async def test_record_turn_writes_through_writer(self):
        from genesis.channels.voice.s2s_session import S2SSessionManager

        writer = MagicMock()
        writer.append_message = AsyncMock()

        bridge = GenesisBridge()
        mgr = S2SSessionManager(bridge=bridge, transcript_writer=writer)
        session = await mgr.get_or_create("sat-1")

        await mgr._record_turn(session, "user", "what time is it")
        await mgr._record_turn(session, "assistant", "half past three")
        assert writer.append_message.await_args_list[0].args == (
            session.session_id,
            "user",
            "what time is it",
        )
        assert writer.append_message.await_args_list[1].args == (
            session.session_id,
            "assistant",
            "half past three",
        )

    async def test_record_turn_skips_blank_and_survives_writer_error(self):
        from genesis.channels.voice.s2s_session import S2SSessionManager

        writer = MagicMock()
        writer.append_message = AsyncMock(side_effect=Exception("disk full"))

        bridge = GenesisBridge()
        mgr = S2SSessionManager(bridge=bridge, transcript_writer=writer)
        session = await mgr.get_or_create("sat-1")

        await mgr._record_turn(session, "user", "   ")
        writer.append_message.assert_not_awaited()
        # Error path must not raise (best-effort recording)
        await mgr._record_turn(session, "user", "hello")

    async def test_record_turn_noop_without_writer(self):
        from genesis.channels.voice.s2s_session import S2SSessionManager

        bridge = GenesisBridge()
        mgr = S2SSessionManager(bridge=bridge)
        session = await mgr.get_or_create("sat-1")
        await mgr._record_turn(session, "user", "hello")  # must not raise


# ─── Voice hours + _should_voice tests ─────────────────────────────────


class TestVoiceHours:
    """Test _in_voice_hours midnight-wrap logic and _should_voice filtering."""

    def _make_pipeline(self, *, voice_hours=(9, 2), has_voice=True, voice_alert_ids=None):
        """Build a minimal pipeline with voice config for testing."""
        from genesis.outreach.config import OutreachConfig, QuietHours
        from genesis.outreach.pipeline import OutreachPipeline

        # When voice_alert_ids is None, use the dataclass default (the
        # shipped 9-item menu) so tests exercise the real allowlist.
        extra = {} if voice_alert_ids is None else {"voice_alert_ids": tuple(voice_alert_ids)}
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
            **extra,
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
            topic="test",
            context="test",
            salience_score=1.0,
            source_id="infra:disk_low",
        )
        with patch.object(pipe, "_in_voice_hours", return_value=True):
            assert not pipe._should_voice(req)

    def test_should_voice_unlisted_is_silent_any_category(self):
        """Pure allowlist: a request with no allowlisted signal_type/source_id
        is silent on voice REGARDLESS of category (the old category gate is
        gone). Covers the behavior change from category-based gating."""
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        pipe = self._make_pipeline()
        for cat in (
            OutreachCategory.SURPLUS,
            OutreachCategory.ALERT,
            OutreachCategory.BLOCKER,
            OutreachCategory.APPROVAL,
        ):
            req = OutreachRequest(
                category=cat,
                topic="t",
                context="t",
                salience_score=1.0,
            )
            with patch.object(pipe, "_in_voice_hours", return_value=True):
                assert not pipe._should_voice(req), f"category={cat}"

    def test_should_voice_allowlisted_source_id(self):
        """A health alert whose source_id is on the allowlist voices."""
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        pipe = self._make_pipeline()
        req = OutreachRequest(
            category=OutreachCategory.BLOCKER,
            topic="t",
            context="t",
            salience_score=1.0,
            signal_type="health_alert",
            source_id="infra:disk_low",
        )
        with patch.object(pipe, "_in_voice_hours", return_value=True):
            assert pipe._should_voice(req)

    def test_should_voice_batched_envelope_any_match(self):
        """Comma-joined batched source_id voices if ANY part is allowlisted."""
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        pipe = self._make_pipeline()
        req = OutreachRequest(
            category=OutreachCategory.BLOCKER,
            topic="t",
            context="t",
            salience_score=1.0,
            signal_type="health_alert",
            source_id="queue:stale_dead_letters,infra:disk_low",
        )
        with patch.object(pipe, "_in_voice_hours", return_value=True):
            assert pipe._should_voice(req)

    def test_should_voice_signal_type_match(self):
        """Non-health signals opt in via signal_type (e.g. sentinel)."""
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        pipe = self._make_pipeline()
        req = OutreachRequest(
            category=OutreachCategory.BLOCKER,
            topic="t",
            context="t",
            salience_score=1.0,
            signal_type="sentinel_escalation",
            source_id="sentinel-escalation:mem:123",
        )
        with patch.object(pipe, "_in_voice_hours", return_value=True):
            assert pipe._should_voice(req)

    def test_should_voice_task_notifications_by_kind(self):
        """Attention-worthy task notifications (task_complete / task_alert) are
        on the default allowlist and voice; routine task_progress is NOT on the
        allowlist and stays silent (Telegram only)."""
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        pipe = self._make_pipeline()
        for signal, expected in (
            ("task_complete", True),
            ("task_alert", True),
            ("task_progress", False),
        ):
            req = OutreachRequest(
                category=OutreachCategory.ALERT,
                topic="t",
                context="t",
                salience_score=1.0,
                signal_type=signal,
                source_id="task:abc123",
            )
            with patch.object(pipe, "_in_voice_hours", return_value=True):
                assert pipe._should_voice(req) is expected, f"signal={signal}"

    def test_should_voice_critical_observation_silent(self):
        """Critical observations (obs UUIDs, not allowlisted) stay silent."""
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        pipe = self._make_pipeline()
        req = OutreachRequest(
            category=OutreachCategory.BLOCKER,
            topic="t",
            context="t",
            salience_score=1.0,
            signal_type="critical_observation",
            source_id="obs-uuid-1,obs-uuid-2",
        )
        with patch.object(pipe, "_in_voice_hours", return_value=True):
            assert not pipe._should_voice(req)

    def test_should_voice_cli_approval_off(self):
        """cli_approval was removed from the allowlist — stays silent."""
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        pipe = self._make_pipeline()
        req = OutreachRequest(
            category=OutreachCategory.APPROVAL,
            topic="t",
            context="t",
            salience_score=1.0,
            signal_type="cli_approval",
            source_id="cli-approval:42",
        )
        with patch.object(pipe, "_in_voice_hours", return_value=True):
            assert not pipe._should_voice(req)

    def test_should_voice_prefix_match_family(self):
        """Prefix matching: a family entry matches suffixed produced ids."""
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        pipe = self._make_pipeline(voice_alert_ids=("provider:credit_exhaustion",))
        req = OutreachRequest(
            category=OutreachCategory.BLOCKER,
            topic="t",
            context="t",
            salience_score=1.0,
            source_id="provider:credit_exhaustion:deepinfra",
        )
        with patch.object(pipe, "_in_voice_hours", return_value=True):
            assert pipe._should_voice(req)

    def test_should_voice_out_of_hours(self):
        """An allowlisted alert outside voice hours is silent."""
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        pipe = self._make_pipeline()
        req = OutreachRequest(
            category=OutreachCategory.BLOCKER,
            topic="t",
            context="t",
            salience_score=1.0,
            source_id="infra:disk_low",
        )
        with patch.object(pipe, "_in_voice_hours", return_value=False):
            assert not pipe._should_voice(req)
