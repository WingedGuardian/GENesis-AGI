"""ToolProvider adapter for TinyFish Fetch — free content extraction.

Fetches 1-10 URLs in parallel, returns clean markdown with metadata.
Server-side anti-bot and JS rendering. Free and unlimited.
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


class TinyFishFetchAdapter:
    """TinyFish Fetch — parallel multi-URL content extraction."""

    name = "tinyfish_fetch"
    capability = ProviderCapability(
        content_types=("web_page", "markdown"),
        categories=(ProviderCategory.WEB,),
        cost_tier=CostTier.FREE,
        description="TinyFish — free parallel URL fetch with anti-bot bypass",
    )

    async def check_health(self) -> ProviderStatus:
        try:
            from genesis.providers.tinyfish_client import _get_key

            _get_key()
            return ProviderStatus.AVAILABLE
        except ValueError:
            return ProviderStatus.UNAVAILABLE

    async def invoke(self, request: dict) -> ProviderResult:
        """Fetch via TinyFish.

        Request keys:
            url (str): Single URL to fetch.
            urls (list[str]): Multiple URLs (1-10) for parallel fetch.
            format (str): "markdown" (default), "html", or "json".
            max_chars (int): Truncate per-URL content (default 50000).
        """
        start = time.monotonic()

        url = request.get("url", "")
        urls = request.get("urls", [])
        if not url and not urls:
            return ProviderResult(
                success=False,
                error="'url' or 'urls' is required in request",
                latency_ms=round((time.monotonic() - start) * 1000, 2),
                provider_name=self.name,
            )

        fetch_urls = urls if urls else [url]
        if len(fetch_urls) > 10:
            fetch_urls = fetch_urls[:10]

        max_chars = request.get("max_chars", 50000)

        try:
            from genesis.providers import tinyfish_client

            response = await tinyfish_client.fetch(
                fetch_urls,
                fmt=request.get("format", "markdown"),
            )

            # Truncate per-URL content
            for item in response.get("results", []):
                text = item.get("text", "")
                if len(text) > max_chars:
                    item["text"] = text[:max_chars]
                    item["truncated"] = True
                else:
                    item["truncated"] = False

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
            logger.error("TinyFish fetch failed", exc_info=True)
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=latency,
                provider_name=self.name,
            )
