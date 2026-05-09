"""Tests for web intelligence MCP tools — web_fetch and web_search."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from genesis.mcp.health.web_tools import (
    _impl_web_fetch,
    _impl_web_search,
    _is_challenge_response,
)
from genesis.web.types import FetchResult, SearchBackend, SearchResponse, SearchResult


class TestIsChallenge:
    def test_403_is_challenge(self):
        assert _is_challenge_response("Forbidden", 403) is True

    def test_429_is_challenge(self):
        assert _is_challenge_response("Rate limited", 429) is True

    def test_503_is_challenge(self):
        assert _is_challenge_response("", 503) is True

    def test_short_cloudflare_text(self):
        assert _is_challenge_response("Please verify you are human cloudflare", 200) is True

    def test_normal_200_not_challenge(self):
        assert _is_challenge_response("A" * 1000, 200) is False

    def test_empty_body_200_not_challenge(self):
        # Empty body with 200 is ambiguous — no markers means not a challenge
        assert _is_challenge_response("", 200) is False

    def test_empty_body_403_is_challenge(self):
        assert _is_challenge_response("", 403) is True


class TestWebFetch:
    @pytest.mark.asyncio
    async def test_missing_url(self):
        result = await _impl_web_fetch("", "auto", 50000)
        assert "error" in result
        assert "required" in result["error"]

    @pytest.mark.asyncio
    async def test_auto_adds_https(self):
        """URLs without scheme get https:// prepended."""
        mock_result = FetchResult(
            url="https://example.com",
            text="Hello world",
            title="Example",
            status_code=200,
        )
        with patch("genesis.mcp.health.web_tools._get_fetcher") as mock:
            mock.return_value.fetch = AsyncMock(return_value=mock_result)
            result = await _impl_web_fetch("example.com", "auto", 50000)
        assert result["content"] == "Hello world"
        assert result["backend_used"] == "scrapling"

    @pytest.mark.asyncio
    async def test_auto_returns_scrapling_result(self):
        mock_result = FetchResult(
            url="https://example.com",
            text="Page content here",
            title="Test Page",
            status_code=200,
        )
        with patch("genesis.mcp.health.web_tools._get_fetcher") as mock:
            mock.return_value.fetch = AsyncMock(return_value=mock_result)
            result = await _impl_web_fetch("https://example.com", "auto", 50000)

        assert result["url"] == "https://example.com"
        assert result["title"] == "Test Page"
        assert result["content"] == "Page content here"
        assert result["backend_used"] == "scrapling"
        assert result["status_code"] == 200
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_auto_escalates_to_crawl4ai_on_challenge(self):
        """When Scrapling gets a 403, should try Crawl4AI."""
        challenge_result = FetchResult(
            url="https://protected.com",
            text="cloudflare challenge",
            title="",
            status_code=403,
        )
        with patch("genesis.mcp.health.web_tools._get_fetcher") as mock_fetcher:
            mock_fetcher.return_value.fetch = AsyncMock(return_value=challenge_result)
            with patch("genesis.mcp.health.web_tools._try_crawl4ai") as mock_crawl:
                mock_crawl.return_value = {
                    "url": "https://protected.com",
                    "title": "Real Page",
                    "content": "JS rendered content",
                    "backend_used": "crawl4ai",
                    "status_code": 200,
                    "truncated": False,
                    "error": None,
                    "latency_ms": 2000.0,
                }
                result = await _impl_web_fetch("https://protected.com", "auto", 50000)

        assert result["backend_used"] == "crawl4ai"
        assert result["content"] == "JS rendered content"

    @pytest.mark.asyncio
    async def test_explicit_crawl4ai_backend(self):
        with patch("genesis.mcp.health.web_tools._try_crawl4ai") as mock:
            mock.return_value = {
                "url": "https://spa.com",
                "title": "SPA",
                "content": "Rendered markdown",
                "backend_used": "crawl4ai",
                "status_code": 200,
                "truncated": False,
                "error": None,
                "latency_ms": 3000.0,
            }
            result = await _impl_web_fetch("https://spa.com", "crawl4ai", 50000)
        assert result["backend_used"] == "crawl4ai"
        assert result["content"] == "Rendered markdown"

    @pytest.mark.asyncio
    async def test_unknown_backend_error(self):
        result = await _impl_web_fetch("https://example.com", "nosuchbackend", 50000)
        assert "error" in result
        assert "Unknown backend" in result["error"]


class TestWebSearch:
    @pytest.mark.asyncio
    async def test_missing_query(self):
        result = await _impl_web_search("", "auto", 10)
        assert "error" in result
        assert "required" in result["error"]

    @pytest.mark.asyncio
    async def test_auto_uses_tinyfish(self):
        mock_response = SearchResponse(
            query="test query",
            results=[
                SearchResult(title="Result 1", url="https://r1.com", snippet="Snippet 1", backend=SearchBackend.TINYFISH),
                SearchResult(title="Result 2", url="https://r2.com", snippet="Snippet 2", backend=SearchBackend.TINYFISH),
            ],
            backend_used=SearchBackend.TINYFISH,
        )
        with patch("genesis.mcp.health.web_tools._get_searcher") as mock:
            mock.return_value.search = AsyncMock(return_value=mock_response)
            result = await _impl_web_search("test query", "auto", 10)

        assert result["query"] == "test query"
        assert result["backend_used"] == "tinyfish"
        assert len(result["results"]) == 2
        assert result["results"][0]["title"] == "Result 1"
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_max_results_capped_at_20(self):
        mock_response = SearchResponse(query="q", results=[], backend_used=SearchBackend.TINYFISH)
        with patch("genesis.mcp.health.web_tools._get_searcher") as mock:
            mock.return_value.search = AsyncMock(return_value=mock_response)
            await _impl_web_search("q", "auto", 100)
            # Verify max_results was capped
            mock.return_value.search.assert_awaited_once_with("q", max_results=20)

    @pytest.mark.asyncio
    async def test_tavily_backend(self):
        with patch("genesis.providers.tavily_adapter.TavilyAdapter") as MockAdapter:
            from genesis.providers.types import ProviderResult

            mock_instance = MockAdapter.return_value
            mock_instance.invoke = AsyncMock(return_value=ProviderResult(
                success=True,
                data={"results": [{"title": "T", "url": "U", "content": "C", "score": 0.9}], "answer": "The answer"},
                provider_name="tavily",
            ))
            result = await _impl_web_search("AI agents", "tavily", 5)

        assert result["backend_used"] == "tavily"
        assert result["answer"] == "The answer"
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_unknown_backend_error(self):
        result = await _impl_web_search("query", "nosuchbackend", 10)
        assert "error" in result
        assert "Unknown backend" in result["error"]
