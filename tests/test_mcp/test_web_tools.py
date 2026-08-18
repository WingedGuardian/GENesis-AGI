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
        """When Scrapling gets a 403 and Ladder is unavailable, should try Crawl4AI."""
        challenge_result = FetchResult(
            url="https://protected.com",
            text="cloudflare challenge",
            title="",
            status_code=403,
        )
        with (
            patch("genesis.mcp.health.web_tools._get_fetcher") as mock_fetcher,
            patch("genesis.mcp.health.web_tools._try_ladder_fetch", return_value=None),
            patch("genesis.mcp.health.web_tools._try_crawl4ai") as mock_crawl,
        ):
            mock_fetcher.return_value.fetch = AsyncMock(return_value=challenge_result)
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
    async def test_auto_uses_ladder_before_crawl4ai_on_challenge(self):
        """When Scrapling gets a challenge, Ladder should be tried before Crawl4AI."""
        challenge_result = FetchResult(
            url="https://protected.com",
            text="cloudflare challenge",
            title="",
            status_code=403,
        )
        with patch("genesis.mcp.health.web_tools._get_fetcher") as mock_fetcher:
            mock_fetcher.return_value.fetch = AsyncMock(return_value=challenge_result)
            with patch("genesis.mcp.health.web_tools._try_ladder_fetch") as mock_ladder:
                mock_ladder.return_value = {
                    "url": "https://protected.com",
                    "title": "",
                    "content": "Ladder bypassed content",
                    "backend_used": "ladder",
                    "status_code": 200,
                    "truncated": False,
                    "error": None,
                    "latency_ms": 500.0,
                }
                with patch("genesis.mcp.health.web_tools._try_crawl4ai") as mock_crawl:
                    result = await _impl_web_fetch("https://protected.com", "auto", 50000)

        assert result["backend_used"] == "ladder"
        assert result["content"] == "Ladder bypassed content"
        mock_crawl.assert_not_called()  # Ladder succeeded, Crawl4AI should not be tried

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
    async def test_auto_uses_searxng(self):
        mock_response = SearchResponse(
            query="test query",
            results=[
                SearchResult(title="Result 1", url="https://r1.com", snippet="Snippet 1", backend=SearchBackend.SEARXNG),
                SearchResult(title="Result 2", url="https://r2.com", snippet="Snippet 2", backend=SearchBackend.SEARXNG),
            ],
            backend_used=SearchBackend.SEARXNG,
        )
        with patch("genesis.mcp.health.web_tools._get_searcher") as mock:
            mock.return_value.search = AsyncMock(return_value=mock_response)
            result = await _impl_web_search("test query", "auto", 10)

        assert result["query"] == "test query"
        assert result["backend_used"] == "searxng"
        assert len(result["results"]) == 2
        assert result["results"][0]["title"] == "Result 1"
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_max_results_capped_at_20(self):
        mock_response = SearchResponse(query="q", results=[], backend_used=SearchBackend.SEARXNG)
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


class TestFirecrawlBackend:
    """Firecrawl = PAID explicit-only escalation backend (never in auto)."""

    @pytest.mark.asyncio
    async def test_explicit_firecrawl_fetch(self, monkeypatch):
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

        async def fake_scrape(url):
            return {
                "markdown": "Rendered paywalled content",
                "metadata": {"title": "Hard Page", "sourceURL": url, "statusCode": 200},
            }

        import genesis.providers.firecrawl_client as fc

        monkeypatch.setattr(fc, "scrape", fake_scrape)
        result = await _impl_web_fetch("https://hard.example", "firecrawl", 50000)
        assert result["backend_used"] == "firecrawl"
        assert result["content"] == "Rendered paywalled content"
        assert result["title"] == "Hard Page"
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_firecrawl_fetch_without_key_errors(self, monkeypatch):
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        result = await _impl_web_fetch("https://hard.example", "firecrawl", 50000)
        assert result["backend_used"] == "firecrawl"
        assert "unavailable" in result["error"]

    @pytest.mark.asyncio
    async def test_firecrawl_never_in_auto_chain(self, monkeypatch):
        """Auto must NEVER touch the paid backend, even with the key set."""
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
        called = []

        async def fake_scrape(url):
            called.append(url)
            return {"markdown": "x", "metadata": {}}

        import genesis.providers.firecrawl_client as fc

        monkeypatch.setattr(fc, "scrape", fake_scrape)
        with patch("genesis.mcp.health.web_tools._try_tinyfish_fetch") as tf:
            tf.return_value = {
                "url": "https://a.com", "title": "t", "content": "c",
                "backend_used": "tinyfish", "status_code": 200,
                "truncated": False, "error": None, "latency_ms": 1.0,
            }
            await _impl_web_fetch("https://a.com", "auto", 50000)
        assert called == [], "auto chain must never burn Firecrawl credits"

    @pytest.mark.asyncio
    async def test_explicit_firecrawl_search(self, monkeypatch):
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

        async def fake_search(query, *, limit=10):
            return [
                {"title": "R1", "url": "https://r1.example", "description": "first"},
                {"title": "R2", "url": "https://r2.example", "description": "second"},
            ]

        import genesis.providers.firecrawl_client as fc

        monkeypatch.setattr(fc, "search", fake_search)
        result = await _impl_web_search("hard query", "firecrawl", 10)
        assert result["backend_used"] == "firecrawl"
        assert [r["title"] for r in result["results"]] == ["R1", "R2"]
        assert result["results"][0]["snippet"] == "first"

    @pytest.mark.asyncio
    async def test_auto_full_escalation_never_reaches_firecrawl(self, monkeypatch):
        """Audit lock: even when EVERY free fetch rung fails/challenges, auto
        must end at the free chain's last rung — never the paid backend."""
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
        called = []

        async def fake_scrape(url):
            called.append(url)
            return {"markdown": "x", "metadata": {}}

        import genesis.providers.firecrawl_client as fc

        monkeypatch.setattr(fc, "scrape", fake_scrape)

        class _ChallengedFetcher:
            async def fetch(self, url, max_chars=50000):
                from types import SimpleNamespace

                return SimpleNamespace(
                    url=url, title="", text="captcha challenge",
                    status_code=403, truncated=False, error=None,
                )

        with (
            patch("genesis.mcp.health.web_tools._try_tinyfish_fetch") as tf,
            patch("genesis.mcp.health.web_tools._try_ladder_fetch") as lf,
            patch("genesis.mcp.health.web_tools._try_crawl4ai") as cf,
            patch("genesis.mcp.health.web_tools._get_fetcher") as gf,
        ):
            tf.return_value = None
            lf.return_value = None
            cf.return_value = None
            gf.return_value = _ChallengedFetcher()
            result = await _impl_web_fetch("https://hard.example", "auto", 50000)
        assert called == [], "exhausted auto chain must not burn paid credits"
        assert result["backend_used"] != "firecrawl"

    @pytest.mark.asyncio
    async def test_search_auto_never_reaches_firecrawl(self, monkeypatch):
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
        called = []

        async def fake_search(query, *, limit=10):
            called.append(query)
            return []

        import genesis.providers.firecrawl_client as fc

        monkeypatch.setattr(fc, "search", fake_search)

        class _EmptySearcher:
            async def search(self, query, max_results=10):
                from types import SimpleNamespace

                return SimpleNamespace(
                    query=query, results=[], backend_used=None,
                    fallback_used=True, error="all free backends failed",
                )

        with (
            patch("genesis.mcp.health.web_tools._try_tinyfish_search") as ts,
            patch("genesis.mcp.health.web_tools._get_searcher") as gs,
        ):
            ts.return_value = None
            gs.return_value = _EmptySearcher()
            result = await _impl_web_search("hard query", "auto", 10)
        assert called == [], "search auto chain must not burn paid credits"
        assert result.get("backend_used") != "firecrawl"
