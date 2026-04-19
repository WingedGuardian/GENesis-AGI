"""Tests for TavilyAdapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from genesis.providers.protocol import ToolProvider
from genesis.providers.tavily_adapter import TavilyAdapter
from genesis.providers.types import (
    CostTier,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)

ENV = {"API_KEY_TAVILY": "tvly-test-key"}


@pytest.fixture
def adapter():
    return TavilyAdapter()


class TestProtocol:
    def test_implements_tool_provider(self, adapter):
        assert isinstance(adapter, ToolProvider)

    def test_name(self, adapter):
        assert adapter.name == "tavily"

    def test_capability_categories(self, adapter):
        assert ProviderCategory.SEARCH in adapter.capability.categories

    def test_capability_cost_tier(self, adapter):
        assert adapter.capability.cost_tier == CostTier.FREE


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_available_when_configured(self, adapter):
        with patch.dict("os.environ", ENV):
            status = await adapter.check_health()
        assert status == ProviderStatus.AVAILABLE

    @pytest.mark.asyncio
    async def test_unavailable_when_no_key(self, adapter):
        with patch.dict("os.environ", {}, clear=True):
            status = await adapter.check_health()
        assert status == ProviderStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_unavailable_when_not_installed(self, adapter):
        with patch.dict("os.environ", ENV), patch.dict("sys.modules", {"tavily": None}):
            status = await adapter.check_health()
        assert status == ProviderStatus.UNAVAILABLE


class TestInvoke:
    @pytest.mark.asyncio
    async def test_missing_query(self, adapter):
        with patch.dict("os.environ", ENV):
            result = await adapter.invoke({})
        assert result.success is False
        assert "query" in result.error.lower()

    @pytest.mark.asyncio
    async def test_missing_key(self, adapter):
        with patch.dict("os.environ", {}, clear=True):
            result = await adapter.invoke({"query": "test"})
        assert result.success is False
        assert "required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_success(self, adapter):
        mock_response = {
            "query": "test query",
            "results": [
                {"title": "Result 1", "url": "https://example.com", "content": "text", "score": 0.95}
            ],
            "answer": "Test answer",
            "response_time": 1.0,
        }
        mock_client = AsyncMock()
        mock_client.search = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", ENV), \
             patch("tavily.AsyncTavilyClient", return_value=mock_client):
            result = await adapter.invoke({"query": "test query"})

        assert isinstance(result, ProviderResult)
        assert result.success is True
        assert result.data["answer"] == "Test answer"
        assert len(result.data["results"]) == 1
        assert result.provider_name == "tavily"

    @pytest.mark.asyncio
    async def test_max_results_capped(self, adapter):
        mock_client = AsyncMock()
        mock_client.search = AsyncMock(return_value={"results": []})

        with patch.dict("os.environ", ENV), \
             patch("tavily.AsyncTavilyClient", return_value=mock_client):
            await adapter.invoke({"query": "test", "max_results": 100})

        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["max_results"] == 20

    @pytest.mark.asyncio
    async def test_generic_error(self, adapter):
        mock_client = AsyncMock()
        mock_client.search = AsyncMock(side_effect=RuntimeError("API error"))

        with patch.dict("os.environ", ENV), \
             patch("tavily.AsyncTavilyClient", return_value=mock_client):
            result = await adapter.invoke({"query": "test"})

        assert result.success is False
        assert "API error" in result.error

    @pytest.mark.asyncio
    async def test_latency_tracked(self, adapter):
        mock_client = AsyncMock()
        mock_client.search = AsyncMock(return_value={"results": []})

        with patch.dict("os.environ", ENV), \
             patch("tavily.AsyncTavilyClient", return_value=mock_client):
            result = await adapter.invoke({"query": "test"})

        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_never_raises(self, adapter):
        mock_client = AsyncMock()
        mock_client.search = AsyncMock(side_effect=Exception("unexpected"))

        with patch.dict("os.environ", ENV), \
             patch("tavily.AsyncTavilyClient", return_value=mock_client):
            result = await adapter.invoke({"query": "test"})

        assert isinstance(result, ProviderResult)
        assert result.success is False
