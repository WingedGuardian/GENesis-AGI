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

    async def search(
        self,
        query: str,
        *,
        max_results: int | None = None,
        backends: tuple[SearchBackend, ...] = (SearchBackend.SEARXNG, SearchBackend.BRAVE),
    ) -> SearchResponse:
        """Search the web, trying ``backends`` in order. Returns SearchResponse (never raises).

        The default order is SearXNG then Brave. Pass a single backend to use
        only that one: an explicit choice must not quietly fall through to a
        different service. On total failure the error names every backend
        tried and why it failed (unreachable, or no key), and ``backend_used``
        stays None; it is set only by the backend that produced the results.
        """
        limit = max_results or self._max_results
        reasons: list[str] = []

        for i, requested in enumerate(backends):
            try:
                backend = SearchBackend(requested)  # accept "brave" as well as the enum
            except ValueError:
                reasons.append(f"{requested}: not a supported search backend")
                continue
            try:
                if backend == SearchBackend.SEARXNG:
                    response = await self._search_searxng(query, limit)
                    results = response.results
                elif backend == SearchBackend.BRAVE:
                    api_key = os.environ.get("API_KEY_BRAVE", "")
                    if not api_key:
                        reasons.append("brave: API_KEY_BRAVE is not set")
                        continue
                    results = await self._search_brave(query, limit, api_key)
                else:  # a future SearchBackend member this loop does not know yet
                    reasons.append(f"{backend.value}: no search implementation")
                    continue
                return SearchResponse(
                    query=query,
                    results=results,
                    backend_used=backend,
                    fallback_used=i > 0,
                )
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                # The reason reaches MCP callers and the voice model, so it carries
                # the exception TYPE (and an HTTP status), never the message: httpx
                # messages can embed the request URL, and SEARXNG_URL may be an
                # internal address. The full message still goes to the log below.
                reason = f"{backend.value}: {type(exc).__name__}"
                if isinstance(exc, httpx.HTTPStatusError):
                    reason += f": HTTP {exc.response.status_code}"
                reasons.append(reason)
                logger.warning("%s search failed (%s)", backend.value, exc)
                if backend == SearchBackend.SEARXNG and self._event_bus:
                    await self._event_bus.emit(
                        Subsystem.WEB,
                        Severity.WARNING,
                        "search.searxng_failed",
                        f"SearXNG failed: {exc}",
                    )

        detail = "; ".join(reasons) or "no backend requested"
        logger.warning("All search backends failed for %r — %s", query, detail)
        if self._event_bus:
            await self._event_bus.emit(
                Subsystem.WEB,
                Severity.ERROR,
                "search.all_failed",
                f"All search backends failed for: {query} ({detail})",
            )
        return SearchResponse(query=query, error=f"All search backends failed — {detail}")

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
        self,
        query: str,
        limit: int,
        api_key: str,
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
