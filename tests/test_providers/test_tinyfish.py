"""Tests for TinyFish adapters (search, fetch, agent) and MCP tool wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from genesis.providers.protocol import ToolProvider
from genesis.providers.tinyfish_agent import COST_PER_STEP_USD, TinyFishAgentAdapter
from genesis.providers.tinyfish_fetch import TinyFishFetchAdapter
from genesis.providers.tinyfish_search import TinyFishSearchAdapter
from genesis.providers.types import (
    CostTier,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)

ENV = {"API_KEY_TINYFISH": "sk-tinyfish-test-key"}


# ── Search adapter ──────────────────────────────────────────────


@pytest.fixture
def search_adapter():
    return TinyFishSearchAdapter()


class TestSearchProtocol:
    def test_implements_tool_provider(self, search_adapter):
        assert isinstance(search_adapter, ToolProvider)

    def test_name(self, search_adapter):
        assert search_adapter.name == "tinyfish_search"

    def test_capability_categories(self, search_adapter):
        assert ProviderCategory.SEARCH in search_adapter.capability.categories

    def test_capability_cost_tier(self, search_adapter):
        assert search_adapter.capability.cost_tier == CostTier.FREE


class TestSearchHealth:
    @pytest.mark.asyncio
    async def test_available_when_configured(self, search_adapter):
        with patch.dict("os.environ", ENV):
            status = await search_adapter.check_health()
        assert status == ProviderStatus.AVAILABLE

    @pytest.mark.asyncio
    async def test_unavailable_when_no_key(self, search_adapter):
        with patch.dict("os.environ", {}, clear=True):
            status = await search_adapter.check_health()
        assert status == ProviderStatus.UNAVAILABLE


class TestSearchInvoke:
    @pytest.mark.asyncio
    async def test_missing_query(self, search_adapter):
        with patch.dict("os.environ", ENV):
            result = await search_adapter.invoke({})
        assert result.success is False
        assert "query" in result.error.lower()

    @pytest.mark.asyncio
    async def test_missing_key(self, search_adapter):
        with patch.dict("os.environ", {}, clear=True):
            result = await search_adapter.invoke({"query": "test"})
        assert result.success is False
        assert "required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_success(self, search_adapter):
        mock_response = {
            "query": "test",
            "results": [
                {"position": 1, "title": "Result 1", "url": "https://example.com", "snippet": "text"}
            ],
            "total_results": 1,
        }
        with patch.dict("os.environ", ENV), \
             patch("genesis.providers.tinyfish_client.search", new_callable=AsyncMock, return_value=mock_response):
            result = await search_adapter.invoke({"query": "test"})

        assert isinstance(result, ProviderResult)
        assert result.success is True
        assert result.data["total_results"] == 1
        assert result.provider_name == "tinyfish_search"
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_api_error(self, search_adapter):
        with patch.dict("os.environ", ENV), \
             patch("genesis.providers.tinyfish_client.search", new_callable=AsyncMock, side_effect=RuntimeError("timeout")):
            result = await search_adapter.invoke({"query": "test"})

        assert result.success is False
        assert "timeout" in result.error


# ── Fetch adapter ───────────────────────────────────────────────


@pytest.fixture
def fetch_adapter():
    return TinyFishFetchAdapter()


class TestFetchProtocol:
    def test_implements_tool_provider(self, fetch_adapter):
        assert isinstance(fetch_adapter, ToolProvider)

    def test_name(self, fetch_adapter):
        assert fetch_adapter.name == "tinyfish_fetch"

    def test_capability_categories(self, fetch_adapter):
        assert ProviderCategory.WEB in fetch_adapter.capability.categories

    def test_capability_cost_tier(self, fetch_adapter):
        assert fetch_adapter.capability.cost_tier == CostTier.FREE


class TestFetchInvoke:
    @pytest.mark.asyncio
    async def test_missing_url(self, fetch_adapter):
        with patch.dict("os.environ", ENV):
            result = await fetch_adapter.invoke({})
        assert result.success is False
        assert "url" in result.error.lower()

    @pytest.mark.asyncio
    async def test_single_url(self, fetch_adapter):
        mock_response = {
            "results": [
                {"url": "https://example.com", "text": "Hello world", "title": "Example", "latency_ms": 500}
            ],
            "errors": [],
        }
        with patch.dict("os.environ", ENV), \
             patch("genesis.providers.tinyfish_client.fetch", new_callable=AsyncMock, return_value=mock_response):
            result = await fetch_adapter.invoke({"url": "https://example.com"})

        assert result.success is True
        assert len(result.data["results"]) == 1
        assert result.data["results"][0]["truncated"] is False

    @pytest.mark.asyncio
    async def test_multi_url(self, fetch_adapter):
        mock_response = {
            "results": [
                {"url": "https://a.com", "text": "A", "title": "A", "latency_ms": 100},
                {"url": "https://b.com", "text": "B", "title": "B", "latency_ms": 200},
            ],
            "errors": [],
        }
        with patch.dict("os.environ", ENV), \
             patch("genesis.providers.tinyfish_client.fetch", new_callable=AsyncMock, return_value=mock_response):
            result = await fetch_adapter.invoke({"urls": ["https://a.com", "https://b.com"]})

        assert result.success is True
        assert len(result.data["results"]) == 2

    @pytest.mark.asyncio
    async def test_truncation(self, fetch_adapter):
        long_text = "x" * 60000
        mock_response = {
            "results": [{"url": "https://example.com", "text": long_text, "title": "Big"}],
            "errors": [],
        }
        with patch.dict("os.environ", ENV), \
             patch("genesis.providers.tinyfish_client.fetch", new_callable=AsyncMock, return_value=mock_response):
            result = await fetch_adapter.invoke({"url": "https://example.com", "max_chars": 1000})

        assert result.data["results"][0]["truncated"] is True
        assert len(result.data["results"][0]["text"]) == 1000

    @pytest.mark.asyncio
    async def test_urls_capped_at_10(self, fetch_adapter):
        urls = [f"https://example{i}.com" for i in range(15)]
        with patch.dict("os.environ", ENV), \
             patch("genesis.providers.tinyfish_client.fetch", new_callable=AsyncMock, return_value={"results": [], "errors": []}) as mock:
            await fetch_adapter.invoke({"urls": urls})

        called_urls = mock.call_args[0][0]
        assert len(called_urls) == 10


# ── Agent adapter ───────────────────────────────────────────────


@pytest.fixture
def agent_adapter():
    return TinyFishAgentAdapter()


class TestAgentProtocol:
    def test_implements_tool_provider(self, agent_adapter):
        assert isinstance(agent_adapter, ToolProvider)

    def test_name(self, agent_adapter):
        assert agent_adapter.name == "tinyfish_agent"

    def test_capability_categories(self, agent_adapter):
        assert ProviderCategory.WEB in agent_adapter.capability.categories
        assert ProviderCategory.EXTRACTION in agent_adapter.capability.categories

    def test_capability_cost_tier(self, agent_adapter):
        assert agent_adapter.capability.cost_tier == CostTier.MODERATE


class TestAgentInvoke:
    @pytest.mark.asyncio
    async def test_missing_url_and_goal(self, agent_adapter):
        with patch.dict("os.environ", ENV):
            result = await agent_adapter.invoke({})
        assert result.success is False
        assert "required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_success(self, agent_adapter):
        mock_response = {
            "run_id": "abc-123",
            "status": "COMPLETED",
            "result": {"data": [1, 2, 3]},
            "num_of_steps": 3,
        }
        with patch.dict("os.environ", ENV), \
             patch("genesis.providers.tinyfish_client.agent_run", new_callable=AsyncMock, return_value=mock_response):
            result = await agent_adapter.invoke({"url": "https://example.com", "goal": "extract data"})

        assert result.success is True
        assert result.data["num_of_steps"] == 3
        assert result.data["cost_usd"] == round(3 * COST_PER_STEP_USD, 4)

    @pytest.mark.asyncio
    async def test_failed_status(self, agent_adapter):
        mock_response = {
            "run_id": "abc-123",
            "status": "FAILED",
            "result": None,
            "error": "Page not found",
            "num_of_steps": 1,
        }
        with patch.dict("os.environ", ENV), \
             patch("genesis.providers.tinyfish_client.agent_run", new_callable=AsyncMock, return_value=mock_response):
            result = await agent_adapter.invoke({"url": "https://example.com", "goal": "extract data"})

        assert result.success is False


# ── MCP tool wiring ─────────────────────────────────────────────


class TestWebSearchAutoChain:
    """Verify TinyFish is first in the auto search chain."""

    @pytest.mark.asyncio
    async def test_auto_uses_tinyfish_when_available(self):
        from genesis.mcp.health.web_tools import _impl_web_search

        mock_response = {
            "results": [{"position": 1, "title": "TF Result", "url": "https://tf.com", "snippet": "found it"}],
            "total_results": 1,
        }
        with patch.dict("os.environ", ENV), \
             patch("genesis.providers.tinyfish_client.search", new_callable=AsyncMock, return_value=mock_response):
            result = await _impl_web_search("test query", "auto", 10)

        assert result["backend_used"] == "tinyfish"
        assert len(result["results"]) == 1
        assert result["results"][0]["title"] == "TF Result"

    @pytest.mark.asyncio
    async def test_auto_falls_back_when_tinyfish_unavailable(self):
        from genesis.mcp.health.web_tools import _impl_web_search

        with patch.dict("os.environ", {}, clear=False), \
             patch("genesis.mcp.health.web_tools._get_searcher") as mock_searcher:
            # Remove TINYFISH key so it falls through
            import os
            old_key = os.environ.pop("API_KEY_TINYFISH", None)
            try:
                mock_response = type("R", (), {
                    "query": "test",
                    "results": [],
                    "backend_used": type("B", (), {"value": "searxng"})(),
                    "fallback_used": False,
                    "error": None,
                })()
                mock_searcher.return_value.search = AsyncMock(return_value=mock_response)
                result = await _impl_web_search("test query", "auto", 10)
            finally:
                if old_key:
                    os.environ["API_KEY_TINYFISH"] = old_key

        assert result["backend_used"] == "searxng"

    @pytest.mark.asyncio
    async def test_score_never_negative(self):
        from genesis.mcp.health.web_tools import _try_tinyfish_search

        mock_response = {
            "results": [{"position": i, "title": f"R{i}", "url": f"https://r{i}.com", "snippet": ""} for i in range(1, 21)],
        }
        with patch.dict("os.environ", ENV), \
             patch("genesis.providers.tinyfish_client.search", new_callable=AsyncMock, return_value=mock_response):
            result = await _try_tinyfish_search("test", 20)

        assert result is not None
        for r in result["results"]:
            assert r["score"] >= 0.0


class TestWebFetchAutoChain:
    """Verify TinyFish is first in the auto fetch chain."""

    @pytest.mark.asyncio
    async def test_auto_uses_tinyfish_when_available(self):
        from genesis.mcp.health.web_tools import _impl_web_fetch

        mock_response = {
            "results": [{"url": "https://example.com", "text": "content here", "title": "Example", "latency_ms": 500}],
            "errors": [],
        }
        with patch.dict("os.environ", ENV), \
             patch("genesis.providers.tinyfish_client.fetch", new_callable=AsyncMock, return_value=mock_response):
            result = await _impl_web_fetch("https://example.com", "auto", 50000)

        assert result["backend_used"] == "tinyfish"
        assert result["content"] == "content here"

    @pytest.mark.asyncio
    async def test_auto_falls_back_to_scrapling(self):
        import os

        from genesis.mcp.health.web_tools import _impl_web_fetch
        old_key = os.environ.pop("API_KEY_TINYFISH", None)
        try:
            mock_result = type("R", (), {
                "url": "https://example.com",
                "title": "Example",
                "text": "scrapling content",
                "status_code": 200,
                "truncated": False,
                "error": None,
            })()
            with patch("genesis.mcp.health.web_tools._get_fetcher") as mock_fetcher:
                mock_fetcher.return_value.fetch = AsyncMock(return_value=mock_result)
                result = await _impl_web_fetch("https://example.com", "auto", 50000)
        finally:
            if old_key:
                os.environ["API_KEY_TINYFISH"] = old_key

        assert result["backend_used"] == "scrapling"


class TestWebFetchMulti:
    """Test multi-URL parallel fetch via urls parameter."""

    @pytest.mark.asyncio
    async def test_multi_url_fetch(self):
        from genesis.mcp.health.web_tools import _impl_web_fetch_multi

        mock_response = {
            "results": [
                {"url": "https://a.com", "text": "A content", "title": "A", "latency_ms": 100},
                {"url": "https://b.com", "text": "B content", "title": "B", "latency_ms": 200},
            ],
            "errors": [],
        }
        with patch.dict("os.environ", ENV), \
             patch("genesis.providers.tinyfish_client.fetch", new_callable=AsyncMock, return_value=mock_response):
            result = await _impl_web_fetch_multi(["https://a.com", "https://b.com"])

        assert result["backend_used"] == "tinyfish"
        assert len(result["results"]) == 2

    @pytest.mark.asyncio
    async def test_multi_url_requires_key(self):
        import os

        from genesis.mcp.health.web_tools import _impl_web_fetch_multi
        old_key = os.environ.pop("API_KEY_TINYFISH", None)
        try:
            result = await _impl_web_fetch_multi(["https://a.com"])
        finally:
            if old_key:
                os.environ["API_KEY_TINYFISH"] = old_key

        assert "error" in result
        assert "API_KEY_TINYFISH" in result["error"]
