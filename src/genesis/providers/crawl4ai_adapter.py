"""ToolProvider adapter for Crawl4AI — local JS-rendering web crawler.

Uses Crawl4AI's AsyncWebCrawler (backed by Playwright) to fetch and
convert web pages to Markdown.  Free, local, no API key needed.
Degrades gracefully if crawl4ai is not installed.
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


class Crawl4AIAdapter:
    """Crawl4AI — local JS-rendered web crawling to Markdown."""

    name = "crawl4ai"
    capability = ProviderCapability(
        content_types=("web_page", "markdown"),
        categories=(ProviderCategory.WEB,),
        cost_tier=CostTier.FREE,
        description="Crawl4AI — local Playwright-based crawl to Markdown (free, no API key)",
    )

    async def check_health(self) -> ProviderStatus:
        """Check if crawl4ai is importable."""
        try:
            import crawl4ai  # noqa: F401

            return ProviderStatus.AVAILABLE
        except ImportError:
            return ProviderStatus.UNAVAILABLE

    async def invoke(self, request: dict) -> ProviderResult:
        """Crawl a URL and return Markdown content.

        Request keys:
            url (str): URL to crawl (required).
        """
        start = time.monotonic()

        url = request.get("url", "")
        if not url:
            return ProviderResult(
                success=False,
                error="'url' is required in request",
                latency_ms=round((time.monotonic() - start) * 1000, 2),
                provider_name=self.name,
            )

        if not url.startswith(("http://", "https://")):
            return ProviderResult(
                success=False,
                error=f"URL must use http:// or https:// scheme, got: {url[:50]}",
                latency_ms=round((time.monotonic() - start) * 1000, 2),
                provider_name=self.name,
            )

        try:
            from crawl4ai import AsyncWebCrawler

            async with AsyncWebCrawler() as crawler:
                result = await crawler.arun(url=url)

            markdown = result.markdown or ""
            latency = round((time.monotonic() - start) * 1000, 2)

            return ProviderResult(
                success=True,
                data=[{"url": url, "markdown": markdown}],
                latency_ms=latency,
                provider_name=self.name,
            )

        except ImportError:
            latency = round((time.monotonic() - start) * 1000, 2)
            return ProviderResult(
                success=False,
                error="crawl4ai is not installed",
                latency_ms=latency,
                provider_name=self.name,
            )
        except Exception as exc:
            latency = round((time.monotonic() - start) * 1000, 2)
            logger.error("Crawl4AI crawl failed for %s", url, exc_info=True)
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=latency,
                provider_name=self.name,
            )
