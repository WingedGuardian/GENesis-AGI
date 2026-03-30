"""Perplexity search adapter.

Uses the Perplexity API (OpenAI-compatible) for search with synthesis.
Requires API_KEY_PERPLEXITY in environment.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

from genesis.providers.types import (
    CostTier,
    ProviderCapability,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)
from genesis.research.types import SearchResult

logger = logging.getLogger(__name__)

PERPLEXITY_ENDPOINT = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_MODEL = "sonar"
PERPLEXITY_TIMEOUT = 30.0


class PerplexityAdapter:
    """Perplexity AI search provider.

    Calls the Perplexity chat/completions API (OpenAI-compatible) and
    returns synthesized answers with citation URLs as SearchResults.
    """

    name = "perplexity"
    capability = ProviderCapability(
        content_types=("web_page", "search_query"),
        categories=(ProviderCategory.SEARCH,),
        cost_tier=CostTier.CHEAP,
        description="Perplexity AI search with synthesis",
    )

    def _get_api_key(self) -> str | None:
        return os.environ.get("API_KEY_PERPLEXITY")

    async def check_health(self) -> ProviderStatus:
        if self._get_api_key():
            return ProviderStatus.AVAILABLE
        return ProviderStatus.UNAVAILABLE

    async def search(self, query: str, *, max_results: int = 10) -> list[SearchResult]:
        """Search via Perplexity API, returning synthesized answer + citations."""
        api_key = self._get_api_key()
        if not api_key:
            logger.error("API_KEY_PERPLEXITY not set")
            return []

        payload = {
            "model": PERPLEXITY_MODEL,
            "messages": [{"role": "user", "content": query}],
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=PERPLEXITY_TIMEOUT) as client:
            resp = await client.post(
                PERPLEXITY_ENDPOINT,
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        # Parse response
        choices = data.get("choices") or []
        answer = ""
        if choices:
            message = choices[0].get("message") or {}
            answer = message.get("content", "")

        citations: list[str] = data.get("citations") or []

        results: list[SearchResult] = []

        # First result is the synthesized answer
        if answer:
            results.append(
                SearchResult(
                    title="Perplexity Answer",
                    url=citations[0] if citations else "",
                    snippet=answer,
                    source="perplexity",
                    score=1.0,
                )
            )

        # Additional results from citations
        for i, url in enumerate(citations[1:max_results], start=2):
            results.append(
                SearchResult(
                    title=f"Citation {i}",
                    url=url,
                    snippet="",
                    source="perplexity",
                    score=0.5,
                )
            )

        return results

    async def invoke(self, request: dict) -> ProviderResult:
        """Invoke search and return a ProviderResult with timing."""
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
            logger.error("Perplexity search failed: %s", exc, exc_info=True)
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=round(latency, 2),
                provider_name=self.name,
            )
