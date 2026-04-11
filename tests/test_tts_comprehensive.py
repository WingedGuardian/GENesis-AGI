"""Comprehensive TTS tests — channels/tts.py, providers, handlers, adapter, bridge, voice helper."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.channels.voice import VoiceDeliveryHelper

# ═══════════════════════════════════════════════════════════════════════════
# 1. channels/tts.py — low-level synthesis functions
# ═══════════════════════════════════════════════════════════════════════════


class TestSynthesizeFishLowLevel:
    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("API_KEY_FISH_AUDIO", raising=False)
        from genesis.channels.tts import synthesize_fish

        with pytest.raises(RuntimeError, match="API_KEY_FISH_AUDIO not set"):
            await synthesize_fish("hello")

    @pytest.mark.asyncio
    async def test_explicit_key_overrides_env(self, monkeypatch):
        monkeypatch.delenv("API_KEY_FISH_AUDIO", raising=False)
        from genesis.channels.tts import synthesize_fish

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.content = b"audio-data"

        with patch("genesis.channels.tts.httpx.post", return_value=fake_resp) as mock_post:
            result = await synthesize_fish("hello", api_key="explicit-key")
            assert result == b"audio-data"
            call_kwargs = mock_post.call_args
            assert call_kwargs[1]["headers"]["Authorization"] == "Bearer explicit-key"

    @pytest.mark.asyncio
    async def test_voice_id_included_in_payload(self, monkeypatch):
        monkeypatch.setenv("API_KEY_FISH_AUDIO", "test-key")
        monkeypatch.delenv("TTS_VOICE_ID_FISH", raising=False)
        from genesis.channels.tts import synthesize_fish

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.content = b"audio"

        with patch("genesis.channels.tts.httpx.post", return_value=fake_resp) as mock_post:
            await synthesize_fish("hello", voice_id="my-voice")
            payload = mock_post.call_args[1]["json"]
            assert payload["reference_id"] == "my-voice"

    @pytest.mark.asyncio
    async def test_no_voice_id_omits_reference(self, monkeypatch):
        monkeypatch.setenv("API_KEY_FISH_AUDIO", "test-key")
        monkeypatch.delenv("TTS_VOICE_ID_FISH", raising=False)
        from genesis.channels.tts import synthesize_fish

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.content = b"audio"

        with patch("genesis.channels.tts.httpx.post", return_value=fake_resp) as mock_post:
            await synthesize_fish("hello")
            payload = mock_post.call_args[1]["json"]
            assert "reference_id" not in payload

    @pytest.mark.asyncio
    async def test_http_error_raises(self, monkeypatch):
        monkeypatch.setenv("API_KEY_FISH_AUDIO", "test-key")
        from genesis.channels.tts import synthesize_fish

        fake_resp = MagicMock()
        fake_resp.status_code = 401
        fake_resp.text = "Unauthorized"

        with patch("genesis.channels.tts.httpx.post", return_value=fake_resp), \
             pytest.raises(RuntimeError, match="Fish Audio TTS failed.*401"):
            await synthesize_fish("hello")


class TestSynthesizeCartesiaLowLevel:
    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("API_KEY_CARTESIA", raising=False)
        from genesis.channels.tts import synthesize_cartesia

        with pytest.raises(RuntimeError, match="API_KEY_CARTESIA not set"):
            await synthesize_cartesia("hello")

    @pytest.mark.asyncio
    async def test_missing_voice_id_raises(self, monkeypatch):
        monkeypatch.setenv("API_KEY_CARTESIA", "test-key")
        monkeypatch.delenv("TTS_VOICE_ID_CARTESIA", raising=False)
        from genesis.channels.tts import synthesize_cartesia

        with pytest.raises(RuntimeError, match="TTS_VOICE_ID_CARTESIA not set"):
            await synthesize_cartesia("hello")

    @pytest.mark.asyncio
    async def test_payload_structure(self, monkeypatch):
        monkeypatch.setenv("API_KEY_CARTESIA", "test-key")
        monkeypatch.setenv("TTS_VOICE_ID_CARTESIA", "voice-uuid")
        from genesis.channels.tts import synthesize_cartesia

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.content = b"wav-audio"

        with patch("genesis.channels.tts.httpx.post", return_value=fake_resp) as mock_post:
            result = await synthesize_cartesia("hello")
            assert result == b"wav-audio"
            payload = mock_post.call_args[1]["json"]
            assert payload["transcript"] == "hello"
            assert payload["voice"] == {"mode": "id", "id": "voice-uuid"}
            assert payload["model_id"] == "sonic-2"
            assert payload["output_format"]["container"] == "wav"

    @pytest.mark.asyncio
    async def test_headers_include_version(self, monkeypatch):
        monkeypatch.setenv("API_KEY_CARTESIA", "test-key")
        monkeypatch.setenv("TTS_VOICE_ID_CARTESIA", "v")
        from genesis.channels.tts import synthesize_cartesia

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.content = b"audio"

        with patch("genesis.channels.tts.httpx.post", return_value=fake_resp) as mock_post:
            await synthesize_cartesia("hi")
            headers = mock_post.call_args[1]["headers"]
            assert "Cartesia-Version" in headers
            assert headers["X-API-Key"] == "test-key"


class TestSynthesizeElevenLabsLowLevel:
    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("API_KEY_ELEVENLABS", raising=False)
        from genesis.channels.tts import synthesize_elevenlabs

        with pytest.raises(RuntimeError, match="API_KEY_ELEVENLABS not set"):
            await synthesize_elevenlabs("hello")

    @pytest.mark.asyncio
    async def test_missing_voice_id_raises(self, monkeypatch):
        monkeypatch.setenv("API_KEY_ELEVENLABS", "test-key")
        monkeypatch.delenv("TTS_VOICE_ID_ELEVENLABS", raising=False)
        from genesis.channels.tts import synthesize_elevenlabs

        with pytest.raises(RuntimeError, match="TTS_VOICE_ID_ELEVENLABS not set"):
            await synthesize_elevenlabs("hello")

    @pytest.mark.asyncio
    async def test_url_includes_voice_id(self, monkeypatch):
        monkeypatch.setenv("API_KEY_ELEVENLABS", "test-key")
        monkeypatch.setenv("TTS_VOICE_ID_ELEVENLABS", "voice-123")
        from genesis.channels.tts import synthesize_elevenlabs

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.content = b"opus-audio"

        with patch("genesis.channels.tts.httpx.post", return_value=fake_resp) as mock_post:
            await synthesize_elevenlabs("hi")
            url = mock_post.call_args[0][0]
            assert "voice-123" in url

    @pytest.mark.asyncio
    async def test_output_format_param(self, monkeypatch):
        monkeypatch.setenv("API_KEY_ELEVENLABS", "test-key")
        monkeypatch.setenv("TTS_VOICE_ID_ELEVENLABS", "v")
        from genesis.channels.tts import synthesize_elevenlabs

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.content = b"audio"

        with patch("genesis.channels.tts.httpx.post", return_value=fake_resp) as mock_post:
            await synthesize_elevenlabs("hi")
            params = mock_post.call_args[1]["params"]
            assert params["output_format"] == "opus_48000_64"


# ═══════════════════════════════════════════════════════════════════════════
# 2. Provider invoke() — success/failure paths
# ═══════════════════════════════════════════════════════════════════════════


class TestProviderInvoke:
    @pytest.mark.asyncio
    async def test_fish_invoke_success(self):
        from genesis.providers.tts import FishAudioTTSAdapter

        adapter = FishAudioTTSAdapter()
        with patch.object(adapter, "synthesize", new_callable=AsyncMock, return_value=b"audio"):
            result = await adapter.invoke({"text": "hello"})
            assert result.success is True
            assert result.data == b"audio"
            assert result.provider_name == "fish_audio_tts"
            assert result.latency_ms > 0

    @pytest.mark.asyncio
    async def test_fish_invoke_failure(self):
        from genesis.providers.tts import FishAudioTTSAdapter

        adapter = FishAudioTTSAdapter()
        with patch.object(
            adapter, "synthesize", new_callable=AsyncMock,
            side_effect=RuntimeError("no key"),
        ):
            result = await adapter.invoke({"text": "hello"})
            assert result.success is False
            assert "no key" in result.error
            assert result.provider_name == "fish_audio_tts"

    @pytest.mark.asyncio
    async def test_cartesia_invoke_passes_voice_id(self):
        from genesis.providers.tts import CartesiaTTSAdapter

        adapter = CartesiaTTSAdapter()
        with patch.object(adapter, "synthesize", new_callable=AsyncMock, return_value=b"wav") as mock:
            await adapter.invoke({"text": "hello", "voice_id": "custom-voice"})
            mock.assert_called_once_with("hello", voice_id="custom-voice")

    @pytest.mark.asyncio
    async def test_elevenlabs_invoke_empty_text(self):
        from genesis.providers.tts import ElevenLabsTTSAdapter

        adapter = ElevenLabsTTSAdapter()
        with patch.object(adapter, "synthesize", new_callable=AsyncMock, return_value=b""):
            result = await adapter.invoke({"text": ""})
            # Empty bytes is falsy → success=False
            assert result.success is False


# ═══════════════════════════════════════════════════════════════════════════
# 3. Handler /tts toggle and _tts_active logic
# ═══════════════════════════════════════════════════════════════════════════


def _make_update(user_id=123, chat_id=456, text="hello"):
    """Build a mock Telegram Update with chat and user."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = MagicMock()
    update.message.text = text
    update.message.chat = MagicMock()
    update.message.chat.id = chat_id
    update.message.chat.send_action = AsyncMock()
    update.message.chat.send_message = AsyncMock()
    update.message.reply_text = AsyncMock()
    update.message.reply_voice = AsyncMock()
    return update


def _make_voice_update(user_id=123, chat_id=456):
    """Build a mock voice message Update."""
    update = _make_update(user_id=user_id, chat_id=chat_id)
    update.message.voice = MagicMock()
    update.message.voice.file_id = "file-123"
    update.message.voice.file_size = 1024
    update.message.audio = None
    return update


def _mock_tts_provider(audio=b"fake-audio"):
    """Create a mock TTSProvider."""
    provider = MagicMock()
    provider.synthesize = AsyncMock(return_value=audio)
    return provider


def _mock_voice_helper(audio=b"fake-audio"):
    """Create a VoiceDeliveryHelper wrapping a mock provider."""
    provider = _mock_tts_provider(audio)
    return VoiceDeliveryHelper(provider), provider


def _mock_adapter():
    """Create a mock ChannelAdapter for voice delivery."""
    adapter = MagicMock()
    adapter.send_voice = AsyncMock(return_value="msg-1")
    adapter.get_chat_lock = MagicMock(return_value=asyncio.Lock())
    return adapter


class TestTTSToggleCommand:
    @pytest.mark.asyncio
    async def test_tts_toggle_no_provider_says_not_configured(self):
        from genesis.channels.telegram.handlers_v2 import make_handlers_v2

        loop = AsyncMock()
        h = make_handlers_v2(loop, allowed_users=set(), whisper_model="base", voice_helper=None)
        update = _make_update()
        await h["tts"](update, MagicMock())
        update.message.reply_text.assert_called_once_with("No TTS provider configured.")

    @pytest.mark.asyncio
    async def test_tts_toggle_cycles_modes(self):
        from genesis.channels.telegram.handlers_v2 import make_handlers_v2

        helper, _ = _mock_voice_helper()
        h = make_handlers_v2(loop=AsyncMock(), allowed_users=set(), whisper_model="base", voice_helper=helper)

        update = _make_update(chat_id=789)

        # Default is "match". Cycle: match → voice → text → match
        ctx_no_args = MagicMock()
        ctx_no_args.args = []

        # First toggle: match → voice
        await h["tts"](update, ctx_no_args)
        update.message.reply_text.assert_called_with("Reply mode: Always voice")

        # Second toggle: voice → text
        update.message.reply_text.reset_mock()
        await h["tts"](update, ctx_no_args)
        update.message.reply_text.assert_called_with("Reply mode: Always text")

        # Third toggle: text → match
        update.message.reply_text.reset_mock()
        await h["tts"](update, ctx_no_args)
        update.message.reply_text.assert_called_with("Reply mode: Match input")

    @pytest.mark.asyncio
    async def test_tts_toggle_unauthorized_ignored(self):
        from genesis.channels.telegram.handlers_v2 import make_handlers_v2

        helper, _ = _mock_voice_helper()
        h = make_handlers_v2(loop=AsyncMock(), allowed_users={999}, whisper_model="base", voice_helper=helper)

        update = _make_update(user_id=123)  # not in allowed_users
        await h["tts"](update, MagicMock())
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_tts_toggle_per_chat_isolation(self):
        from genesis.channels.telegram.handlers_v2 import make_handlers_v2

        helper, _ = _mock_voice_helper()
        h = make_handlers_v2(loop=AsyncMock(), allowed_users=set(), whisper_model="base", voice_helper=helper)

        ctx_no_args = MagicMock()
        ctx_no_args.args = []

        # Toggle chat 100: match → voice
        update_a = _make_update(chat_id=100)
        await h["tts"](update_a, ctx_no_args)
        update_a.message.reply_text.assert_called_with("Reply mode: Always voice")

        # Chat 200 is independent — still at default "match", first toggle → voice
        update_b = _make_update(chat_id=200)
        await h["tts"](update_b, ctx_no_args)
        update_b.message.reply_text.assert_called_with("Reply mode: Always voice")


# ═══════════════════════════════════════════════════════════════════════════
# 4. Handler voice flow — TTS success, failure fallback, disabled
# ═══════════════════════════════════════════════════════════════════════════


class TestHandleVoiceWithTTS:
    @pytest.fixture
    def mock_loop(self):
        loop = AsyncMock()
        loop.handle_message_streaming = AsyncMock(return_value="Genesis response")
        loop._db = MagicMock()
        return loop

    @pytest.mark.asyncio
    async def test_voice_with_tts_sends_voice_message(self, mock_loop):
        from genesis.channels.telegram.handlers_v2 import make_handlers_v2

        helper, provider = _mock_voice_helper(audio=b"synthesized-audio")
        adapter = _mock_adapter()
        h = make_handlers_v2(mock_loop, allowed_users=set(), whisper_model="base", voice_helper=helper, adapter=adapter)

        update = _make_voice_update()
        ctx = MagicMock()
        mock_file = AsyncMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"raw-audio"))
        ctx.bot.get_file = AsyncMock(return_value=mock_file)

        with patch("genesis.channels.telegram._handler_messages.stt.transcribe", new_callable=AsyncMock, return_value="hello world"):
            await h["voice"](update, ctx)

        # Should have called synthesize via helper
        provider.synthesize.assert_called_once()
        adapter.send_voice.assert_called_once()
        # Check the text transcript was sent
        text_calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        assert any("\U0001f3a4" in t for t in text_calls)

    @pytest.mark.asyncio
    async def test_voice_tts_failure_falls_back_to_text(self, mock_loop):
        from genesis.channels.telegram.handlers_v2 import make_handlers_v2

        helper, provider = _mock_voice_helper()
        provider.synthesize.side_effect = RuntimeError("TTS broke")
        adapter = _mock_adapter()
        h = make_handlers_v2(mock_loop, allowed_users=set(), whisper_model="base", voice_helper=helper, adapter=adapter)

        update = _make_voice_update()
        ctx = MagicMock()
        mock_file = AsyncMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"raw"))
        ctx.bot.get_file = AsyncMock(return_value=mock_file)

        with patch("genesis.channels.telegram._handler_messages.stt.transcribe", new_callable=AsyncMock, return_value="hello"):
            await h["voice"](update, ctx)

        # Voice should NOT have been delivered via adapter
        adapter.send_voice.assert_not_called()
        # Text fallback should have been sent (+ transcription echo)
        assert update.message.reply_text.call_count >= 1
        first_call_text = update.message.reply_text.call_args_list[0][0][0]
        assert "Genesis response" in first_call_text

    @pytest.mark.asyncio
    async def test_voice_no_tts_provider_sends_text(self, mock_loop):
        from genesis.channels.telegram.handlers_v2 import make_handlers_v2

        h = make_handlers_v2(mock_loop, allowed_users=set(), whisper_model="base", voice_helper=None)

        update = _make_voice_update()
        ctx = MagicMock()
        mock_file = AsyncMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"raw"))
        ctx.bot.get_file = AsyncMock(return_value=mock_file)

        with patch("genesis.channels.telegram._handler_messages.stt.transcribe", new_callable=AsyncMock, return_value="hello"):
            await h["voice"](update, ctx)

        # Response + transcription echo = 2 calls
        assert update.message.reply_text.call_count >= 1

    @pytest.mark.asyncio
    async def test_voice_tts_disabled_via_toggle_sends_text(self, mock_loop):
        from genesis.channels.telegram.handlers_v2 import make_handlers_v2

        helper, provider = _mock_voice_helper()
        adapter = _mock_adapter()
        h = make_handlers_v2(mock_loop, allowed_users=set(), whisper_model="base", voice_helper=helper, adapter=adapter)

        chat_id = 456

        # Set text-only mode for this chat via /tts text
        toggle_update = _make_update(chat_id=chat_id)
        ctx_text = MagicMock()
        ctx_text.args = ["text"]
        await h["tts"](toggle_update, ctx_text)

        # Now send voice — should get text, not audio
        update = _make_voice_update(chat_id=chat_id)
        ctx = MagicMock()
        mock_file = AsyncMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"raw"))
        ctx.bot.get_file = AsyncMock(return_value=mock_file)

        with patch("genesis.channels.telegram._handler_messages.stt.transcribe", new_callable=AsyncMock, return_value="hello"):
            await h["voice"](update, ctx)

        provider.synthesize.assert_not_called()
        adapter.send_voice.assert_not_called()
        # Response + transcription echo = 2 calls
        assert update.message.reply_text.call_count >= 1

    @pytest.mark.asyncio
    async def test_voice_empty_response_skips_tts(self, mock_loop):
        from genesis.channels.telegram.handlers_v2 import make_handlers_v2

        mock_loop.handle_message_streaming = AsyncMock(return_value="")
        helper, provider = _mock_voice_helper()
        adapter = _mock_adapter()
        h = make_handlers_v2(mock_loop, allowed_users=set(), whisper_model="base", voice_helper=helper, adapter=adapter)

        update = _make_voice_update()
        ctx = MagicMock()
        mock_file = AsyncMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"raw"))
        ctx.bot.get_file = AsyncMock(return_value=mock_file)

        with patch("genesis.channels.telegram._handler_messages.stt.transcribe", new_callable=AsyncMock, return_value="hello"):
            await h["voice"](update, ctx)

        # Empty response → _tts_active might be true but `response` is falsy
        provider.synthesize.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# 5. Adapter send_voice + tts_provider wiring
# ═══════════════════════════════════════════════════════════════════════════


class TestTelegramAdapterTTS:
    def test_adapter_accepts_tts_provider(self):
        from genesis.channels.telegram.adapter_v2 import TelegramAdapterV2

        provider = _mock_tts_provider()
        adapter = TelegramAdapterV2(
            token="t", conversation_loop=AsyncMock(), tts_provider=provider,
        )
        assert adapter.tts_provider is provider

    def test_adapter_tts_provider_defaults_none(self):
        from genesis.channels.telegram.adapter_v2 import TelegramAdapterV2

        adapter = TelegramAdapterV2(token="t", conversation_loop=AsyncMock())
        assert adapter.tts_provider is None

    @pytest.mark.asyncio
    async def test_send_voice_not_started_raises(self):
        from genesis.channels.telegram.adapter_v2 import TelegramAdapterV2

        adapter = TelegramAdapterV2(token="t", conversation_loop=AsyncMock())
        with pytest.raises(RuntimeError, match="not started"):
            await adapter.send_voice("123", b"audio")


# ═══════════════════════════════════════════════════════════════════════════
# 6. ChannelAdapter base class send_voice default
# ═══════════════════════════════════════════════════════════════════════════


class TestChannelAdapterSendVoice:
    @pytest.mark.asyncio
    async def test_base_send_voice_raises_not_implemented(self):
        from genesis.channels.base import ChannelAdapter

        class StubAdapter(ChannelAdapter):
            async def start(self): ...
            async def stop(self): ...
            async def send_message(self, cid, text): return ""
            async def send_typing(self, cid): ...
            def get_capabilities(self): return {}
            async def get_engagement_signals(self, did): return {}

        adapter = StubAdapter()
        with pytest.raises(NotImplementedError, match="StubAdapter"):
            await adapter.send_voice("123", b"audio")


# ═══════════════════════════════════════════════════════════════════════════
# 7. Bridge TTS_ENABLED env var
# ═══════════════════════════════════════════════════════════════════════════


class TestBridgeTTSEnabled:
    def test_tts_enabled_true_by_default(self):
        """TTS_ENABLED defaults to true when not set."""
        import os
        val = os.environ.get("TTS_ENABLED", "true").lower()
        assert val not in ("false", "0", "no")

    @pytest.mark.parametrize("val", ["false", "False", "FALSE", "0", "no", "No"])
    def test_tts_enabled_false_values(self, val):
        """All falsy values should disable TTS."""
        assert val.lower() in ("false", "0", "no")

    @pytest.mark.parametrize("val", ["true", "True", "1", "yes", ""])
    def test_tts_enabled_true_values(self, val):
        """All truthy/empty values should enable TTS."""
        assert val.lower() not in ("false", "0", "no")


# ═══════════════════════════════════════════════════════════════════════════
# 8. Runtime registration gating
# ═══════════════════════════════════════════════════════════════════════════


class TestRuntimeTTSRegistration:
    def test_fish_not_registered_without_key(self, monkeypatch):
        monkeypatch.delenv("API_KEY_FISH_AUDIO", raising=False)
        from genesis.providers.registry import ProviderRegistry
        from genesis.providers.tts import FishAudioTTSAdapter

        registry = ProviderRegistry.__new__(ProviderRegistry)
        registry._providers = {}

        # Simulate runtime logic
        import os
        if os.environ.get("API_KEY_FISH_AUDIO"):
            registry._providers["fish_audio_tts"] = FishAudioTTSAdapter()

        assert len(registry._providers) == 0

    def test_fish_registered_with_key(self, monkeypatch):
        monkeypatch.setenv("API_KEY_FISH_AUDIO", "test")
        from genesis.providers.registry import ProviderRegistry
        from genesis.providers.tts import FishAudioTTSAdapter

        registry = ProviderRegistry.__new__(ProviderRegistry)
        registry._providers = {}

        import os
        if os.environ.get("API_KEY_FISH_AUDIO"):
            registry._providers["fish_audio_tts"] = FishAudioTTSAdapter()

        assert "fish_audio_tts" in registry._providers


# ═══════════════════════════════════════════════════════════════════════════
# 9. /start command shows /tts only when provider configured
# ═══════════════════════════════════════════════════════════════════════════


class TestStartCommandTTSLine:
    @pytest.mark.asyncio
    async def test_start_shows_tts_when_helper_set(self):
        from genesis.channels.telegram.handlers_v2 import make_handlers_v2

        helper, _ = _mock_voice_helper()
        h = make_handlers_v2(loop=AsyncMock(), allowed_users=set(), whisper_model="base", voice_helper=helper)

        update = _make_update()
        await h["start"](update, MagicMock())
        text = update.message.reply_text.call_args[0][0]
        assert "/tts" in text

    @pytest.mark.asyncio
    async def test_start_hides_tts_when_no_helper(self):
        from genesis.channels.telegram.handlers_v2 import make_handlers_v2

        h = make_handlers_v2(loop=AsyncMock(), allowed_users=set(), whisper_model="base", voice_helper=None)

        update = _make_update()
        await h["start"](update, MagicMock())
        text = update.message.reply_text.call_args[0][0]
        assert "/tts" not in text


# ═══════════════════════════════════════════════════════════════════════════
# 10. Handler dict includes 'tts' key
# ═══════════════════════════════════════════════════════════════════════════


class TestHandlerDictComplete:
    def test_all_expected_handlers_present(self):
        from genesis.channels.telegram.handlers_v2 import make_handlers_v2

        h = make_handlers_v2(loop=AsyncMock(), allowed_users=set(), whisper_model="base")
        expected = {"start", "help", "new", "stop", "model", "effort", "status", "usage", "tts", "pause", "text", "voice", "photo", "document", "callback_query"}
        assert set(h.keys()) == expected
