"""ToolProvider adapter for TinyFish Search — free web search.

Returns structured results with titles, snippets, and URLs.
Free and unlimited on all plans.
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

logger = logging.getLogger(__name__)


class TinyFishSearchAdapter:
    """TinyFish Search — fast web search with structured results."""

    name = "tinyfish_search"
    capability = ProviderCapability(
        content_types=("search_results",),
        categories=(ProviderCategory.SEARCH,),
        cost_tier=CostTier.FREE,
        description="TinyFish — free web search, 2x faster than SearXNG",
    )

    async def check_health(self) -> ProviderStatus:
        try:
            from genesis.providers.tinyfish_client import _get_key

            _get_key()
            return ProviderStatus.AVAILABLE
        except ValueError:
            return ProviderStatus.UNAVAILABLE

    async def invoke(self, request: dict) -> ProviderResult:
        """Search via TinyFish.

        Request keys:
            query (str): Search query (required).
            max_results (int): Ignored (TinyFish returns 10 results).
            location (str): Country code hint (optional).
            language (str): Language code hint (optional).
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
            from genesis.providers import tinyfish_client

            response = await tinyfish_client.search(
                query,
                location=request.get("location"),
                language=request.get("language"),
            )

            latency = round((time.monotonic() - start) * 1000, 2)
            return ProviderResult(
                success=True,
                data=response,
                latency_ms=latency,
                provider_name=self.name,
            )
        except ValueError as exc:
            latency = round((time.monotonic() - start) * 1000, 2)
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=latency,
                provider_name=self.name,
            )
        except Exception as exc:
            latency = round((time.monotonic() - start) * 1000, 2)
            logger.error("TinyFish search failed", exc_info=True)
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=latency,
                provider_name=self.name,
            )
