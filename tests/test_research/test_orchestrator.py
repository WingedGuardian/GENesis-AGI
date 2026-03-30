"""Tests for genesis.research.orchestrator."""

from unittest.mock import AsyncMock

import pytest

from genesis.providers.registry import ProviderRegistry
from genesis.providers.types import (
    CostTier,
    ProviderCapability,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)
from genesis.research.orchestrator import ResearchOrchestrator
from genesis.research.types import SearchResult
from genesis.routing.types import RoutingResult


class FakeSearchProvider:
    def __init__(self, name, results=None, *, should_fail=False):
        self.name = name
        self.capability = ProviderCapability(
            content_types=("search_query",),
            categories=(ProviderCategory.SEARCH,),
            cost_tier=CostTier.FREE,
        )
        self._results = results or []
        self._should_fail = should_fail

    async def check_health(self):
        return ProviderStatus.AVAILABLE

    async def invoke(self, request):
        return ProviderResult(success=True, data=self._results, provider_name=self.name)

    async def search(self, query, *, max_results=10):
        if self._should_fail:
            raise ConnectionError("down")
        return self._results[:max_results]


class TestOrchestrator:
    def _make_result(self, title, url, source="test"):
        return SearchResult(title=title, url=url, snippet="", source=source)

    @pytest.mark.asyncio
    async def test_single_provider(self):
        reg = ProviderRegistry()
        results = [self._make_result("A", "http://a.com")]
        reg.register(FakeSearchProvider("p1", results))

        orch = ResearchOrchestrator(registry=reg)
        res = await orch.search("test")
        assert len(res.results) == 1
        assert res.sources_queried == ["p1"]

    @pytest.mark.asyncio
    async def test_multi_provider_dedup(self):
        reg = ProviderRegistry()
        r1 = [self._make_result("A", "http://a.com"), self._make_result("B", "http://b.com")]
        r2 = [self._make_result("A dup", "http://a.com"), self._make_result("C", "http://c.com")]
        reg.register(FakeSearchProvider("p1", r1))
        reg.register(FakeSearchProvider("p2", r2))

        orch = ResearchOrchestrator(registry=reg)
        res = await orch.search("test")
        assert len(res.results) == 3  # a, b, c (a deduped)
        assert res.deduplicated_count == 1
        urls = {r.url for r in res.results}
        assert urls == {"http://a.com", "http://b.com", "http://c.com"}

    @pytest.mark.asyncio
    async def test_no_providers(self):
        reg = ProviderRegistry()
        orch = ResearchOrchestrator(registry=reg)
        res = await orch.search("test")
        assert res.results == []
        assert res.sources_queried == []

    @pytest.mark.asyncio
    async def test_provider_failure_graceful(self):
        reg = ProviderRegistry()
        reg.register(FakeSearchProvider("good", [self._make_result("A", "http://a.com")]))
        reg.register(FakeSearchProvider("bad", should_fail=True))

        orch = ResearchOrchestrator(registry=reg)
        res = await orch.search("test")
        assert len(res.results) == 1  # good provider's result

    @pytest.mark.asyncio
    async def test_named_providers(self):
        reg = ProviderRegistry()
        reg.register(FakeSearchProvider("p1", [self._make_result("A", "http://a.com")]))
        reg.register(FakeSearchProvider("p2", [self._make_result("B", "http://b.com")]))

        orch = ResearchOrchestrator(registry=reg)
        res = await orch.search("test", providers=["p1"])
        assert len(res.results) == 1
        assert res.sources_queried == ["p1"]

    @pytest.mark.asyncio
    async def test_max_results_respected(self):
        reg = ProviderRegistry()
        many = [self._make_result(f"R{i}", f"http://{i}.com") for i in range(20)]
        reg.register(FakeSearchProvider("p1", many))

        orch = ResearchOrchestrator(registry=reg)
        res = await orch.search("test", max_results=5)
        assert len(res.results) == 5

    @pytest.mark.asyncio
    async def test_search_and_synthesize_returns_results(self):
        """No router provided — synthesis stays None."""
        reg = ProviderRegistry()
        reg.register(FakeSearchProvider("p1", [self._make_result("A", "http://a.com")]))

        orch = ResearchOrchestrator(registry=reg)
        res = await orch.search_and_synthesize("test")
        assert len(res.results) == 1
        assert res.synthesis is None

    @pytest.mark.asyncio
    async def test_search_and_synthesize_with_router(self):
        """Router returns content — synthesis is populated."""
        reg = ProviderRegistry()
        reg.register(FakeSearchProvider("p1", [self._make_result("A", "http://a.com")]))

        mock_router = AsyncMock()
        mock_router.route_call.return_value = RoutingResult(
            success=True,
            call_site_id="34_research_synthesis",
            provider_used="test-provider",
            content="Synthesized summary of results.",
            input_tokens=50,
            output_tokens=20,
            cost_usd=0.001,
        )

        orch = ResearchOrchestrator(registry=reg)
        res = await orch.search_and_synthesize("test", router=mock_router)
        assert len(res.results) == 1
        assert res.synthesis == "Synthesized summary of results."
        mock_router.route_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_and_synthesize_router_failure(self):
        """Router raises — graceful fallback, synthesis stays None."""
        reg = ProviderRegistry()
        reg.register(FakeSearchProvider("p1", [self._make_result("A", "http://a.com")]))

        mock_router = AsyncMock()
        mock_router.route_call.side_effect = RuntimeError("LLM down")

        orch = ResearchOrchestrator(registry=reg)
        res = await orch.search_and_synthesize("test", router=mock_router)
        assert len(res.results) == 1
        assert res.synthesis is None
