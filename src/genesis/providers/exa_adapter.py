"""ToolProvider adapter for Exa — neural/semantic web search.

Uses embedding-based search to find conceptually related content,
even when exact keywords aren't known. Free tier: 1,000 searches/month.
"""

from __future__ import annotations

import logging
import os
import time

from genesis.providers.types import (
    CostTier,
    ProviderCapability,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)

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
