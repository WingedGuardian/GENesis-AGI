"""Tests for genesis.providers.health."""

from genesis.providers.health import OllamaProbeAdapter, QdrantProbeAdapter
from genesis.providers.protocol import ToolProvider
from genesis.providers.types import ProviderCategory


class TestQdrantProbeAdapter:
    def test_is_tool_provider(self):
        adapter = QdrantProbeAdapter()
        assert isinstance(adapter, ToolProvider)

    def test_capability(self):
        adapter = QdrantProbeAdapter()
        assert ProviderCategory.HEALTH in adapter.capability.categories


class TestOllamaProbeAdapter:
    def test_is_tool_provider(self):
        adapter = OllamaProbeAdapter()
        assert isinstance(adapter, ToolProvider)

    def test_capability(self):
        adapter = OllamaProbeAdapter()
        assert ProviderCategory.HEALTH in adapter.capability.categories
