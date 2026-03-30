"""Tests for genesis.providers.tts — protocol compliance, capabilities, health."""

import pytest

from genesis.providers.protocol import ToolProvider
from genesis.providers.tts import (
    CartesiaTTSAdapter,
    ElevenLabsTTSAdapter,
    FishAudioTTSAdapter,
    TTSProvider,
)
from genesis.providers.types import ProviderCategory, ProviderStatus


class TestFishAudioTTSAdapter:
    def test_is_tool_provider(self):
        adapter = FishAudioTTSAdapter()
        assert isinstance(adapter, ToolProvider)

    def test_capability(self):
        adapter = FishAudioTTSAdapter()
        assert ProviderCategory.TTS in adapter.capability.categories
        assert "text" in adapter.capability.content_types

    @pytest.mark.asyncio
    async def test_health_no_key(self, monkeypatch):
        monkeypatch.delenv("API_KEY_FISH_AUDIO", raising=False)
        adapter = FishAudioTTSAdapter()
        assert await adapter.check_health() == ProviderStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_health_with_key(self, monkeypatch):
        monkeypatch.setenv("API_KEY_FISH_AUDIO", "test")
        adapter = FishAudioTTSAdapter()
        assert await adapter.check_health() == ProviderStatus.AVAILABLE


class TestCartesiaTTSAdapter:
    def test_is_tool_provider(self):
        adapter = CartesiaTTSAdapter()
        assert isinstance(adapter, ToolProvider)

    def test_capability(self):
        adapter = CartesiaTTSAdapter()
        assert ProviderCategory.TTS in adapter.capability.categories
        assert "text" in adapter.capability.content_types

    @pytest.mark.asyncio
    async def test_health_no_key(self, monkeypatch):
        monkeypatch.delenv("API_KEY_CARTESIA", raising=False)
        adapter = CartesiaTTSAdapter()
        assert await adapter.check_health() == ProviderStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_health_with_key(self, monkeypatch):
        monkeypatch.setenv("API_KEY_CARTESIA", "test")
        adapter = CartesiaTTSAdapter()
        assert await adapter.check_health() == ProviderStatus.AVAILABLE


class TestElevenLabsTTSAdapter:
    def test_is_tool_provider(self):
        adapter = ElevenLabsTTSAdapter()
        assert isinstance(adapter, ToolProvider)

    def test_capability(self):
        adapter = ElevenLabsTTSAdapter()
        assert ProviderCategory.TTS in adapter.capability.categories
        assert "text" in adapter.capability.content_types

    @pytest.mark.asyncio
    async def test_health_no_key(self, monkeypatch):
        monkeypatch.delenv("API_KEY_ELEVENLABS", raising=False)
        adapter = ElevenLabsTTSAdapter()
        assert await adapter.check_health() == ProviderStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_health_with_key(self, monkeypatch):
        monkeypatch.setenv("API_KEY_ELEVENLABS", "test")
        adapter = ElevenLabsTTSAdapter()
        assert await adapter.check_health() == ProviderStatus.AVAILABLE


class TestTTSProtocol:
    """Verify all adapters satisfy TTSProvider protocol."""

    @pytest.mark.parametrize(
        "cls",
        [FishAudioTTSAdapter, CartesiaTTSAdapter, ElevenLabsTTSAdapter],
    )
    def test_implements_tts_protocol(self, cls):
        adapter = cls()
        assert isinstance(adapter, TTSProvider)
