"""Tests for genesis.pipeline.collectors."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.pipeline.collectors import (
    Collector,
    CollectorRegistry,
    WebSearchCollector,
)
from genesis.pipeline.types import CollectorResult


class TestCollectorRegistry:
    def test_register_and_create(self):
        registry = CollectorRegistry()

        class DummyCollector:
            name = "dummy"

            def __init__(self, profile_name: str, **kwargs):
                self.profile_name = profile_name

            async def collect(self, queries, *, max_results=20):
                return CollectorResult(collector_name="dummy", signals=[])

        registry.register("dummy", DummyCollector)
        collector = registry.create("dummy", profile_name="test")
        assert isinstance(collector, DummyCollector)
        assert collector.profile_name == "test"

    def test_unknown_collector_raises(self):
        registry = CollectorRegistry()
        with pytest.raises(KeyError):
            registry.create("nonexistent", profile_name="test")

    def test_register_overwrites(self):
        registry = CollectorRegistry()

        class A:
            def __init__(self, profile_name, **kw):
                pass

        class B:
            def __init__(self, profile_name, **kw):
                pass

        registry.register("x", A)
        registry.register("x", B)
        assert registry.create("x", profile_name="t").__class__ is B

    def test_builtin_web_search_registered(self):
        registry = CollectorRegistry()
        assert "web_search" in registry.available()

    def test_available_lists_all(self):
        registry = CollectorRegistry()
        registry.register("custom", type)
        names = registry.available()
        assert "web_search" in names
        assert "custom" in names


class TestWebSearchCollector:
    async def test_returns_signals_with_correct_structure(self):
        collector = WebSearchCollector(profile_name="test")

        mock_result = MagicMock()
        mock_result.title = "Test Title"
        mock_result.snippet = "Test snippet"
        mock_result.url = "https://example.com"

        mock_response = MagicMock()
        mock_response.results = [mock_result]

        with patch("genesis.web.search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = mock_response
            result = await collector.collect(["test query"], max_results=5)

        assert isinstance(result, CollectorResult)
        assert len(result.signals) == 1
        assert result.signals[0].source == "web_search"
        assert result.signals[0].profile_name == "test"
        assert "Test Title" in result.signals[0].content
        assert result.signals[0].url == "https://example.com"

    async def test_handles_search_errors_gracefully(self):
        collector = WebSearchCollector(profile_name="test")

        with patch("genesis.web.search", new_callable=AsyncMock) as mock_search:
            mock_search.side_effect = RuntimeError("Connection failed")
            result = await collector.collect(["failing query"])

        assert isinstance(result, CollectorResult)
        assert len(result.signals) == 0
        assert len(result.errors) == 1
        assert "Connection failed" in result.errors[0]


class TestCollectorProtocol:
    def test_web_search_collector_satisfies_protocol(self):
        collector = WebSearchCollector(profile_name="test")
        assert isinstance(collector, Collector)
