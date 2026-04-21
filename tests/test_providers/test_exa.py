"""Tests for ExaAdapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("exa_py")

from genesis.providers.exa_adapter import ExaAdapter
from genesis.providers.protocol import ToolProvider
from genesis.providers.types import (
    CostTier,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)

ENV = {"API_KEY_EXA": "exa-test-key"}


@pytest.fixture
def adapter():
    return ExaAdapter()


class TestProtocol:
    def test_implements_tool_provider(self, adapter):
        assert isinstance(adapter, ToolProvider)

    def test_name(self, adapter):
        assert adapter.name == "exa"

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
        with patch.dict("os.environ", ENV), patch.dict("sys.modules", {"exa_py": None}):
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
        mock_result_item = MagicMock()
        mock_result_item.url = "https://example.com"
        mock_result_item.title = "Example"
        mock_result_item.score = 0.95
        mock_result_item.text = "Full text content"
        mock_result_item.highlights = ["key excerpt"]

        mock_response = MagicMock()
        mock_response.results = [mock_result_item]

        mock_client = AsyncMock()
        mock_client.search = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", ENV), \
             patch("exa_py.AsyncExa", return_value=mock_client):
            result = await adapter.invoke({"query": "test query"})

        assert isinstance(result, ProviderResult)
        assert result.success is True
        assert len(result.data["results"]) == 1
        assert result.data["results"][0]["url"] == "https://example.com"
        assert result.data["results"][0]["text"] == "Full text content"
        assert result.provider_name == "exa"

    @pytest.mark.asyncio
    async def test_num_results_capped(self, adapter):
        mock_response = MagicMock()
        mock_response.results = []
        mock_client = AsyncMock()
        mock_client.search = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", ENV), \
             patch("exa_py.AsyncExa", return_value=mock_client):
            await adapter.invoke({"query": "test", "num_results": 100})

        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["num_results"] == 20

    @pytest.mark.asyncio
    async def test_domain_filters_passed(self, adapter):
        mock_response = MagicMock()
        mock_response.results = []
        mock_client = AsyncMock()
        mock_client.search = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", ENV), \
             patch("exa_py.AsyncExa", return_value=mock_client):
            await adapter.invoke({
                "query": "test",
                "include_domains": ["example.com"],
                "exclude_domains": ["spam.com"],
            })

        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["include_domains"] == ["example.com"]
        assert call_kwargs["exclude_domains"] == ["spam.com"]

    @pytest.mark.asyncio
    async def test_generic_error(self, adapter):
        mock_client = AsyncMock()
        mock_client.search = AsyncMock(side_effect=RuntimeError("API error"))

        with patch.dict("os.environ", ENV), \
             patch("exa_py.AsyncExa", return_value=mock_client):
            result = await adapter.invoke({"query": "test"})

        assert result.success is False
        assert "API error" in result.error

    @pytest.mark.asyncio
    async def test_never_raises(self, adapter):
        mock_client = AsyncMock()
        mock_client.search = AsyncMock(side_effect=Exception("unexpected"))

        with patch.dict("os.environ", ENV), \
             patch("exa_py.AsyncExa", return_value=mock_client):
            result = await adapter.invoke({"query": "test"})

        assert isinstance(result, ProviderResult)
        assert result.success is False
