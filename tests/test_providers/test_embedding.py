"""Tests for genesis.providers.embedding."""

import pytest

from genesis.providers.embedding import CloudEmbeddingAdapter, OllamaEmbeddingAdapter
from genesis.providers.protocol import ToolProvider
from genesis.providers.types import ProviderCategory, ProviderStatus


class TestOllamaEmbeddingAdapter:
    def test_is_tool_provider(self):
        adapter = OllamaEmbeddingAdapter()
        assert isinstance(adapter, ToolProvider)

    def test_capability(self):
        adapter = OllamaEmbeddingAdapter()
        assert ProviderCategory.EMBEDDING in adapter.capability.categories


class TestCloudEmbeddingAdapter:
    def test_is_tool_provider(self):
        adapter = CloudEmbeddingAdapter(provider="deepinfra")
        assert isinstance(adapter, ToolProvider)

    @pytest.mark.asyncio
    async def test_health_no_key(self, monkeypatch):
        monkeypatch.delenv("API_KEY_DEEPINFRA", raising=False)
        adapter = CloudEmbeddingAdapter(provider="deepinfra")
        assert await adapter.check_health() == ProviderStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_health_with_key(self, monkeypatch):
        monkeypatch.setenv("API_KEY_DEEPINFRA", "test")
        adapter = CloudEmbeddingAdapter(provider="deepinfra")
        assert await adapter.check_health() == ProviderStatus.AVAILABLE

    @pytest.mark.asyncio
    async def test_dashscope_health_no_key(self, monkeypatch):
        monkeypatch.delenv("API_KEY_QWEN", raising=False)
        adapter = CloudEmbeddingAdapter(provider="dashscope")
        assert await adapter.check_health() == ProviderStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_dashscope_health_with_key(self, monkeypatch):
        monkeypatch.setenv("API_KEY_QWEN", "test")
        adapter = CloudEmbeddingAdapter(provider="dashscope")
        assert await adapter.check_health() == ProviderStatus.AVAILABLE
