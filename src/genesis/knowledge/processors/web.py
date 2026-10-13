"""Web content processor wrapping the existing WebFetcher.

Escalates to Cloudflare Browser Run /markdown when the primary fetch
returns thin content (JS-rendered shell with no real text).
"""

from __future__ import annotations

import logging
import os
import re

from genesis.knowledge.processors.base import ProcessedContent
from genesis.security.sanitizer import strip_boundary_markers

logger = logging.getLogger(__name__)

# The single wrapper WebFetcher puts around a page: a complete opener at the very
# start and the closer carrying the SAME id at the very end. Only that exact pair is
# removed; text that merely looks like a marker is page content.
_FETCH_WRAPPER_RE = re.compile(
    r'\A<external-content [^<>]*\bid="([0-9a-f]{16})"[^<>]*>\n'
    r'(?P<body>.*)\n</external-content id="\1">\Z',
    re.DOTALL,
)


def _unwrap_fetched(text: str) -> str:
    match = _FETCH_WRAPPER_RE.match(text)
    return match.group("body") if match else text
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
        # WebFetcher wraps the whole page in exactly one boundary block. Remove
        # that outer wrapper here, where it is known to be the fetcher's: its id
        # depends on the install's boundary key, so leaving it in would make the
        # ingest content hash change whenever the key does (#2572). Distillation
        # wraps every chunk again before it reaches a model, and marker-shaped
        # text inside the page is left untouched.
        text = _unwrap_fetched(result.text)
        title = result.title
        escalated = False
        stripped = strip_boundary_markers(text).strip()
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
