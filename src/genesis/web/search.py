"""Web search via SearXNG (primary) with Brave fallback."""

from __future__ import annotations

import logging
import os

import httpx

from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem
from genesis.security import ContentSanitizer, ContentSource
from genesis.web.types import SearchBackend, SearchResponse, SearchResult

logger = logging.getLogger(__name__)

_SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:55510/search")
_BRAVE_URL = os.environ.get("BRAVE_API_URL", "https://api.search.brave.com/res/v1/web/search")
_SANITIZER = ContentSanitizer()


class WebSearcher:
    """Async web searcher: SearXNG primary, Brave Search API fallback."""

    def __init__(
        self,
        *,
        searxng_url: str = _SEARXNG_URL,
        brave_url: str = _BRAVE_URL,
        timeout_s: float = 15.0,
        max_results: int = 10,
        event_bus: GenesisEventBus | None = None,
    ) -> None:
        self._searxng_url = searxng_url
        self._brave_url = brave_url
        self._max_results = max_results
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._event_bus = event_bus

    async def search(self, query: str, *, max_results: int | None = None) -> SearchResponse:
        """Search the web. Returns SearchResponse (never raises)."""
        limit = max_results or self._max_results

        # Try SearXNG
        try:
            return await self._search_searxng(query, limit)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("SearXNG search failed (%s), falling back to Brave", exc)
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.WEB, Severity.WARNING, "search.searxng_failed",
                    f"SearXNG failed: {exc}",
                )

        # Try Brave
        api_key = os.environ.get("API_KEY_BRAVE", "")
        if not api_key:
            logger.warning("Brave API key not configured, search unavailable")
            return SearchResponse(query=query, error="All search backends unavailable")

        try:
            results = await self._search_brave(query, limit, api_key)
            return SearchResponse(
                query=query, results=results,
                backend_used=SearchBackend.BRAVE, fallback_used=True,
            )
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("Brave search also failed (%s)", exc)
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.WEB, Severity.ERROR, "search.all_failed",
                    f"All search backends failed for: {query}",
                )
            return SearchResponse(query=query, error=f"All search backends failed: {exc}")

    async def _search_searxng(self, query: str, limit: int) -> SearchResponse:
        resp = await self._client.post(
            self._searxng_url,
            data={"q": query, "format": "json"},
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("results", [])[:limit]
        results = [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=_SANITIZER.wrap_content(r.get("content", ""), ContentSource.WEB_SEARCH),
                backend=SearchBackend.SEARXNG,
                score=float(r.get("score", 0.0)),
            )
            for r in raw
        ]
        return SearchResponse(query=query, results=results, backend_used=SearchBackend.SEARXNG)

    async def _search_brave(
        self, query: str, limit: int, api_key: str,
    ) -> list[SearchResult]:
        resp = await self._client.get(
            self._brave_url,
            params={"q": query, "count": min(limit, 20)},
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("web", {}).get("results", [])[:limit]
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=_SANITIZER.wrap_content(r.get("description", ""), ContentSource.WEB_SEARCH),
                backend=SearchBackend.BRAVE,
            )
            for r in raw
        ]
