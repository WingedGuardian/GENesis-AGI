"""Tests for genesis.research.web_adapter."""

from unittest.mock import AsyncMock

import pytest

from genesis.providers.protocol import ToolProvider
from genesis.providers.types import ProviderCategory, ProviderStatus
from genesis.research.web_adapter import WebSearchAdapter
from genesis.web.types import SearchBackend, SearchResponse
from genesis.web.types import SearchResult as WebSearchResult


class TestWebSearchAdapter:
    def test_is_tool_provider(self):
        adapter = WebSearchAdapter()
        assert isinstance(adapter, ToolProvider)

    def test_capability(self):
        adapter = WebSearchAdapter()
        assert ProviderCategory.SEARCH in adapter.capability.categories
        assert "search_query" in adapter.capability.content_types

    @pytest.mark.asyncio
    async def test_search_delegates_to_web_searcher(self):
        mock_searcher = AsyncMock()
        mock_searcher.search.return_value = SearchResponse(
            query="test",
            results=[
                WebSearchResult(title="A", url="http://a.com", snippet="snip", backend=SearchBackend.SEARXNG),
            ],
            backend_used=SearchBackend.SEARXNG,
        )

        adapter = WebSearchAdapter(searcher=mock_searcher)
        results = await adapter.search("test", max_results=5)

        mock_searcher.search.assert_called_once_with("test", max_results=5)
        assert len(results) == 1
        assert results[0].title == "A"
        assert results[0].source == "searxng"

    @pytest.mark.asyncio
    async def test_invoke(self):
        mock_searcher = AsyncMock()
        mock_searcher.search.return_value = SearchResponse(
            query="q",
            results=[
                WebSearchResult(title="B", url="http://b.com", snippet="s", backend=SearchBackend.BRAVE),
            ],
            backend_used=SearchBackend.BRAVE,
        )

        adapter = WebSearchAdapter(searcher=mock_searcher)
        result = await adapter.invoke({"query": "q", "max_results": 3})
        assert result.success
        assert len(result.data) == 1

    @pytest.mark.asyncio
    async def test_invoke_empty(self):
        mock_searcher = AsyncMock()
        mock_searcher.search.return_value = SearchResponse(query="q", results=[])

        adapter = WebSearchAdapter(searcher=mock_searcher)
        result = await adapter.invoke({"query": "q"})
        assert not result.success  # no results

    @pytest.mark.asyncio
    async def test_health_check_available(self):
        mock_searcher = AsyncMock()
        mock_searcher.search.return_value = SearchResponse(
            query="test",
            results=[WebSearchResult(title="X", url="http://x", snippet="", backend=SearchBackend.SEARXNG)],
        )
        adapter = WebSearchAdapter(searcher=mock_searcher)
        status = await adapter.check_health()
        assert status == ProviderStatus.AVAILABLE

    @pytest.mark.asyncio
    async def test_health_check_unavailable_on_error(self):
        mock_searcher = AsyncMock()
        mock_searcher.search.return_value = SearchResponse(
            query="test", error="all backends down",
        )
        adapter = WebSearchAdapter(searcher=mock_searcher)
        status = await adapter.check_health()
        assert status == ProviderStatus.UNAVAILABLE
