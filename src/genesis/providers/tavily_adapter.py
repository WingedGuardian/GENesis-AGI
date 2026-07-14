"""ToolProvider adapter for Tavily — AI-optimized web search.

Returns structured search results designed for LLM consumption.
Free tier: 1,000 searches/month (no credit card required).
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

from genesis.providers.types import (
    CostTier,
    ProviderCapability,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)

if TYPE_CHECKING:
    from genesis.research.types import SearchResult

logger = logging.getLogger(__name__)


class TavilyAdapter:
    """Tavily — AI-optimized web search for agent pipelines."""

    name = "tavily"
    capability = ProviderCapability(
        content_types=("search_results", "web_page"),
        categories=(ProviderCategory.SEARCH,),
        cost_tier=CostTier.FREE,
        description="Tavily — AI-optimized search with 1K free searches/month",
    )

    def __init__(self) -> None:
        self._client = None

    def _get_key(self) -> str:
        key = os.environ.get("API_KEY_TAVILY", "")
        if not key:
            raise ValueError("API_KEY_TAVILY required")
        return key

    async def check_health(self) -> ProviderStatus:
        """Check if Tavily is configured and reachable."""
        try:
            self._get_key()
        except ValueError:
            return ProviderStatus.UNAVAILABLE

        try:
            from tavily import AsyncTavilyClient  # noqa: F401

            return ProviderStatus.AVAILABLE
        except ImportError:
            return ProviderStatus.UNAVAILABLE

    async def invoke(self, request: dict) -> ProviderResult:
        """Search via Tavily.

        Request keys:
            query (str): Search query (required).
            max_results (int): Max results (default 5, max 20).
            search_depth (str): 'basic' or 'advanced' (default 'basic').
            include_answer (bool): Include LLM-generated answer (default True).
            topic (str): 'general', 'news', or 'finance' (default 'general').
        """
        start = time.monotonic()

        query = request.get("query", "")
        if not query:
            return ProviderResult(
                success=False,
                error="'query' is required in request",
                latency_ms=round((time.monotonic() - start) * 1000, 2),
                provider_name=self.name,
            )

        try:
            key = self._get_key()
        except ValueError as exc:
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=round((time.monotonic() - start) * 1000, 2),
                provider_name=self.name,
            )

        try:
            from tavily import AsyncTavilyClient

            client = AsyncTavilyClient(api_key=key)
            response = await client.search(
                query=query,
                max_results=min(request.get("max_results", 5), 20),
                search_depth=request.get("search_depth", "basic"),
                include_answer=request.get("include_answer", True),
                topic=request.get("topic", "general"),
            )

            latency = round((time.monotonic() - start) * 1000, 2)
            return ProviderResult(
                success=True,
                data=response,
                latency_ms=latency,
                provider_name=self.name,
            )

        except ImportError:
            latency = round((time.monotonic() - start) * 1000, 2)
            return ProviderResult(
                success=False,
                error="tavily-python is not installed",
                latency_ms=latency,
                provider_name=self.name,
            )
        except Exception as exc:
            latency = round((time.monotonic() - start) * 1000, 2)
            logger.error("Tavily search failed", exc_info=True)
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=latency,
                provider_name=self.name,
            )

    async def search(self, query: str, *, max_results: int = 10) -> list[SearchResult]:
        """Return normalized SearchResult objects for the research orchestrator.

        Wraps ``invoke()`` (which returns the raw Tavily dict) and maps each
        ``results[]`` entry's title/url/content/score into a SearchResult tagged
        ``source="tavily"``. Returns ``[]`` on any failure so a degraded provider
        never breaks the fan-out — matching WebSearchAdapter/PerplexityAdapter.
        Without this method the orchestrator fell back to ``invoke()`` and its
        ``isinstance(result.data, list)`` guard silently dropped the dict.
        """
        from genesis.research.types import SearchResult

        result = await self.invoke({"query": query, "max_results": max_results})
        if not result.success or not isinstance(result.data, dict):
            return []
        out: list[SearchResult] = []
        for entry in result.data.get("results", []):
            url = entry.get("url")
            if not url:
                continue
            try:
                score = float(entry.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            out.append(
                SearchResult(
                    title=entry.get("title") or "",
                    url=url,
                    snippet=entry.get("content") or "",
                    source=self.name,
                    score=score,
                )
            )
        return out
