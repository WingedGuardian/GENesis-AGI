"""Tests for genesis.providers.stt."""

import pytest

from genesis.providers.protocol import ToolProvider
from genesis.providers.stt import GroqSTTAdapter
from genesis.providers.types import ProviderCategory, ProviderStatus


class TestGroqSTTAdapter:
    def test_is_tool_provider(self):
        adapter = GroqSTTAdapter()
        assert isinstance(adapter, ToolProvider)

    def test_capability(self):
        adapter = GroqSTTAdapter()
        assert ProviderCategory.STT in adapter.capability.categories
        assert "audio" in adapter.capability.content_types

    @pytest.mark.asyncio
    async def test_health_no_key(self, monkeypatch):
        monkeypatch.delenv("API_KEY_GROQ", raising=False)
        adapter = GroqSTTAdapter()
        assert await adapter.check_health() == ProviderStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_health_with_key(self, monkeypatch):
        monkeypatch.setenv("API_KEY_GROQ", "test")
        adapter = GroqSTTAdapter()
        assert await adapter.check_health() == ProviderStatus.AVAILABLE
