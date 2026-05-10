"""ToolProvider adapter wrapping genesis.web.WebSearcher.

Instead of duplicating SearXNG/Brave logic, we wrap the existing
genesis.web infrastructure as a single SearchProvider that the
ProviderRegistry and ResearchOrchestrator can use.
"""

from __future__ import annotations

import logging
import time

from genesis.providers.types import (
    CostTier,
    ProviderCapability,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)
from genesis.research.types import SearchResult

logger = logging.getLogger(__name__)


class WebSearchAdapter:
    """Wraps genesis.web.WebSearcher as a ToolProvider.

    SearXNG primary + Brave fallback is handled internally by WebSearcher.
    """

    name = "web_search"
    capability = ProviderCapability(
        content_types=("web_page", "search_query"),
        categories=(ProviderCategory.SEARCH,),
        cost_tier=CostTier.FREE,
        description="Web search via SearXNG (primary) + Brave (fallback)",
    )

    def __init__(self, searcher=None) -> None:
        self._searcher = searcher

    def _get_searcher(self):
        if self._searcher is None:
            from genesis.web.search import WebSearcher
            self._searcher = WebSearcher()
        return self._searcher

    async def check_health(self) -> ProviderStatus:
        try:
            searcher = self._get_searcher()
            response = await searcher.search("test", max_results=1)
            if response.error:
                return ProviderStatus.UNAVAILABLE
            return ProviderStatus.AVAILABLE
        except Exception:
            return ProviderStatus.UNAVAILABLE

    async def search(self, query: str, *, max_results: int = 10) -> list[SearchResult]:
        searcher = self._get_searcher()
        response = await searcher.search(query, max_results=max_results)
        return [
            SearchResult(
                title=r.title,
                url=r.url,
                snippet=r.snippet,
                source=str(response.backend_used),
                score=r.score,
            )
            for r in response.results
        ]

    async def invoke(self, request: dict) -> ProviderResult:
        start = time.monotonic()
        try:
            query = request.get("query", request.get("q", ""))
            max_results = request.get("max_results", 10)
            results = await self.search(query, max_results=max_results)
            latency = (time.monotonic() - start) * 1000
            return ProviderResult(
                success=bool(results),
                data=results,
                latency_ms=round(latency, 2),
                provider_name=self.name,
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=round(latency, 2),
                provider_name=self.name,
            )
