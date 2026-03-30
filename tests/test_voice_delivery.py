"""Tests for VoiceDeliveryHelper — synthesize+deliver pattern."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.channels.voice import VoiceDeliveryHelper


def _make_helper(audio=b"fake-audio", config_loader=None):
    provider = MagicMock()
    provider.synthesize = AsyncMock(return_value=audio)
    adapter = MagicMock()
    adapter.send_voice = AsyncMock(return_value="msg-1")
    helper = VoiceDeliveryHelper(provider, config_loader)
    return helper, provider, adapter


class TestSynthesizeAndDeliver:
    @pytest.mark.asyncio
    async def test_success_path(self):
        helper, provider, adapter = _make_helper()
        result = await helper.synthesize_and_deliver(adapter, "123", "hello")
        assert result is True
        provider.synthesize.assert_called_once()
        adapter.send_voice.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_text_returns_false(self):
        helper, provider, adapter = _make_helper()
        result = await helper.synthesize_and_deliver(adapter, "123", "")
        assert result is False
        provider.synthesize.assert_not_called()

    @pytest.mark.asyncio
    async def test_synthesis_failure_returns_false(self):
        helper, provider, adapter = _make_helper()
        provider.synthesize.side_effect = RuntimeError("API down")
        result = await helper.synthesize_and_deliver(adapter, "123", "hello")
        assert result is False
        adapter.send_voice.assert_not_called()

    @pytest.mark.asyncio
    async def test_delivery_failure_returns_false(self):
        helper, provider, adapter = _make_helper()
        adapter.send_voice.side_effect = RuntimeError("send failed")
        result = await helper.synthesize_and_deliver(adapter, "123", "hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_adapter_timeout_absorbed_is_success(self):
        """When the adapter absorbs a timeout (returns normally), helper succeeds."""
        helper, provider, adapter = _make_helper()
        # Adapter handles TimedOut internally and returns a delivery ID
        adapter.send_voice = AsyncMock(return_value="timeout-likely-delivered")
        result = await helper.synthesize_and_deliver(adapter, "123", "hello")
        assert result is True

    @pytest.mark.asyncio
    async def test_reply_to_message_id_passed_through(self):
        helper, provider, adapter = _make_helper()
        await helper.synthesize_and_deliver(
            adapter, "123", "hello", reply_to_message_id="42"
        )
        adapter.send_voice.assert_called_once_with(
            "123", b"fake-audio", reply_to_message_id="42"
        )

    @pytest.mark.asyncio
    async def test_sanitization_applied(self):
        helper, provider, adapter = _make_helper()
        await helper.synthesize_and_deliver(adapter, "123", "**bold text**")
        # sanitize_for_speech strips bold markers
        call_text = provider.synthesize.call_args[0][0]
        assert "**" not in call_text
        assert "bold text" in call_text

    @pytest.mark.asyncio
    async def test_sanitization_with_config_loader(self):
        from genesis.channels.tts_config import SanitizationSettings, TTSConfig

        config = TTSConfig(sanitization=SanitizationSettings(max_chars=5))
        loader = MagicMock()
        loader.load.return_value = config

        helper, provider, adapter = _make_helper(config_loader=loader)
        await helper.synthesize_and_deliver(adapter, "123", "a" * 100)
        call_text = provider.synthesize.call_args[0][0]
        assert len(call_text) == 5

    @pytest.mark.asyncio
    async def test_empty_audio_returns_false(self):
        helper, provider, adapter = _make_helper(audio=b"")
        result = await helper.synthesize_and_deliver(adapter, "123", "hello")
        assert result is False
        adapter.send_voice.assert_not_called()


class TestVoiceDeliveryHelperProperties:
    def test_available_true(self):
        helper, _, _ = _make_helper()
        assert helper.available is True

    def test_available_false_with_none_provider(self):
        # Provider is required by __init__, but available checks truthiness
        helper = VoiceDeliveryHelper(provider=None)
        assert helper.available is False
