"""ToolProvider adapter for Exa — neural/semantic web search.

Uses embedding-based search to find conceptually related content,
even when exact keywords aren't known. Free tier: 1,000 searches/month.
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


class ExaAdapter:
    """Exa — neural/semantic web search + answer generation."""

    name = "exa"
    capability = ProviderCapability(
        content_types=("search_results", "web_page"),
        categories=(ProviderCategory.SEARCH,),
        cost_tier=CostTier.FREE,
        description="Exa — neural search with 1K free searches/month",
    )

    def __init__(self) -> None:
        self._client = None

    def _get_key(self) -> str:
        key = os.environ.get("API_KEY_EXA", "")
        if not key:
            raise ValueError("API_KEY_EXA required")
        return key

    async def check_health(self) -> ProviderStatus:
        """Check if Exa is configured and reachable."""
        try:
            self._get_key()
        except ValueError:
            return ProviderStatus.UNAVAILABLE

        try:
            from exa_py import Exa  # noqa: F401

            return ProviderStatus.AVAILABLE
        except ImportError:
            return ProviderStatus.UNAVAILABLE

    async def invoke(self, request: dict) -> ProviderResult:
        """Search via Exa.

        Request keys:
            query (str): Search query (required).
            num_results (int): Max results (default 5, max 20).
            contents (dict): Content options, e.g. {"highlights": True, "text": True}.
            include_domains (list[str]): Limit to these domains.
            exclude_domains (list[str]): Exclude these domains.
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
            from exa_py import AsyncExa

            client = AsyncExa(api_key=key)

            kwargs: dict = {
                "query": query,
                "num_results": min(request.get("num_results", 5), 20),
            }
            if request.get("contents"):
                kwargs["contents"] = request["contents"]
            if request.get("include_domains"):
                kwargs["include_domains"] = request["include_domains"]
            if request.get("exclude_domains"):
                kwargs["exclude_domains"] = request["exclude_domains"]

            response = await client.search(**kwargs)

            # Normalize to dict for consistent ProviderResult
            results = []
            for r in response.results:
                entry = {
                    "url": r.url,
                    "title": r.title,
                    "score": getattr(r, "score", None),
                }
                if hasattr(r, "text") and r.text:
                    entry["text"] = r.text
                if hasattr(r, "highlights") and r.highlights:
                    entry["highlights"] = r.highlights
                results.append(entry)

            latency = round((time.monotonic() - start) * 1000, 2)
            return ProviderResult(
                success=True,
                data={"results": results, "query": query},
                latency_ms=latency,
                provider_name=self.name,
            )

        except ImportError:
            latency = round((time.monotonic() - start) * 1000, 2)
            return ProviderResult(
                success=False,
                error="exa-py is not installed",
                latency_ms=latency,
                provider_name=self.name,
            )
        except Exception as exc:
            latency = round((time.monotonic() - start) * 1000, 2)
            logger.error("Exa search failed", exc_info=True)
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=latency,
                provider_name=self.name,
            )

    async def search(self, query: str, *, max_results: int = 10) -> list[SearchResult]:
        """Return normalized SearchResult objects for the research orchestrator.

        Wraps ``invoke()`` (raw Exa dict) and maps each result's url/title/score
        plus text (or highlights) into a SearchResult tagged ``source="exa"``.
        Bridges the orchestrator's ``max_results`` onto Exa's ``num_results`` key
        (the fallback path passed ``max_results``, which Exa ignored — always
        defaulting to 5) and requests capped page text so snippets reach parity
        with the other providers. Returns ``[]`` on any failure.
        """
        from genesis.research.types import SearchResult

        result = await self.invoke(
            {
                "query": query,
                "num_results": max_results,
                "contents": {"text": {"max_characters": 500}},
            }
        )
        if not result.success or not isinstance(result.data, dict):
            return []
        out: list[SearchResult] = []
        for entry in result.data.get("results", []):
            url = entry.get("url")
            if not url:
                continue
            snippet = entry.get("text") or " ".join(entry.get("highlights") or []) or ""
            try:
                score = float(entry.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            out.append(
                SearchResult(
                    title=entry.get("title") or "",
                    url=url,
                    snippet=snippet,
                    source=self.name,
                    score=score,
                )
            )
        return out
