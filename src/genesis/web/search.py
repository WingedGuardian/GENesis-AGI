"""Web search via Tinyfish (primary) with Brave fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx

from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem
from genesis.security import ContentSanitizer, ContentSource
from genesis.web.types import SearchBackend, SearchResponse, SearchResult

logger = logging.getLogger(__name__)

_BRAVE_URL = os.environ.get("BRAVE_API_URL", "https://api.search.brave.com/res/v1/web/search")
_SANITIZER = ContentSanitizer()


class WebSearcher:
    """Async web searcher: Tinyfish primary, Brave Search API fallback."""

    def __init__(
        self,
        *,
        brave_url: str = _BRAVE_URL,
        timeout_s: float = 15.0,
        max_results: int = 10,
        event_bus: GenesisEventBus | None = None,
    ) -> None:
        self._brave_url = brave_url
        self._max_results = max_results
        self._timeout_s = timeout_s
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._event_bus = event_bus

    async def search(self, query: str, *, max_results: int | None = None) -> SearchResponse:
        """Search the web. Returns SearchResponse (never raises)."""
        limit = max_results or self._max_results

        # Try Tinyfish (free, cloud, reliable)
        try:
            return await self._search_tinyfish(query, limit)
        except Exception as exc:
            logger.warning("Tinyfish search failed (%s), falling back to Brave", exc)
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.WEB, Severity.WARNING, "search.tinyfish_failed",
                    f"Tinyfish failed: {exc}",
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

    async def _search_tinyfish(self, query: str, limit: int) -> SearchResponse:
        """Search via Tinyfish CLI (free tier, 30 req/min)."""
        proc = await asyncio.create_subprocess_exec(
            "tinyfish", "search", "query", query,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=self._timeout_s,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"tinyfish exited {proc.returncode}: {stderr.decode()[:200]}"
            )
        data = json.loads(stdout.decode())
        raw = data.get("results", [])[:limit]
        results = [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=_SANITIZER.wrap_content(
                    r.get("snippet", ""), ContentSource.WEB_SEARCH,
                ),
                backend=SearchBackend.TINYFISH,
                score=float(r.get("position", 0)),
            )
            for r in raw
        ]
        return SearchResponse(
            query=query, results=results, backend_used=SearchBackend.TINYFISH,
        )

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
