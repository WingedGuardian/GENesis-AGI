"""Web content processor wrapping the existing WebFetcher.

Escalates to Cloudflare Browser Run /markdown when the primary fetch
returns thin content (JS-rendered shell with no real text).
"""

from __future__ import annotations

import logging
import os
import re

from genesis.knowledge.processors.base import ProcessedContent

logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r"^https?://")
_THIN_CONTENT_THRESHOLD = 200  # chars after stripping markers


class WebProcessor:
    """Fetch and extract content from web URLs."""

    async def process(self, source: str, **kwargs: object) -> ProcessedContent:
        from genesis.web.fetch import WebFetcher

        fetcher = WebFetcher()
        result = await fetcher.fetch(source)

        if result.error:
            raise RuntimeError(f"Failed to fetch {source}: {result.error}")

        # Escalate to Cloudflare /markdown if the primary fetch returned
        # thin content (likely a JS-rendered shell like <div id="root">).
        text = result.text
        title = result.title
        escalated = False
        stripped = re.sub(r"<external-content[^>]*>|</external-content>", "", text).strip()
        if len(stripped) < _THIN_CONTENT_THRESHOLD:
            cf_text = await self._try_cloudflare_markdown(source)
            if cf_text:
                text = cf_text
                escalated = True
                logger.info("Escalated to Cloudflare /markdown for %s", source)

        return ProcessedContent(
            text=text,
            metadata={
                "url": result.url,
                "title": title,
                "status_code": result.status_code,
                "truncated": result.truncated,
                "escalated_to_cloudflare": escalated,
            },
            source_type="web",
            source_path=source,
        )

    def can_handle(self, source: str) -> bool:
        return bool(_URL_PATTERN.match(source))

    @staticmethod
    async def _try_cloudflare_markdown(url: str) -> str | None:
        """Attempt Cloudflare /markdown extraction. Returns None on failure."""
        if not os.environ.get("API_KEY_CLOUDFLARE") or not os.environ.get("CLOUDFLARE_ACCOUNT_ID"):
            return None
        try:
            from genesis.providers.cloudflare_crawl import CloudflareCrawlAdapter

            adapter = CloudflareCrawlAdapter()
            result = await adapter.fetch_markdown(url)
            if result.success and result.data:
                return result.data if isinstance(result.data, str) else str(result.data)
        except Exception:
            logger.debug("Cloudflare /markdown escalation failed for %s", url, exc_info=True)
        return None
