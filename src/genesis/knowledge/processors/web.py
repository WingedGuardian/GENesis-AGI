"""Web content processor wrapping the existing WebFetcher."""

from __future__ import annotations

import logging
import re

from genesis.knowledge.processors.base import ProcessedContent

logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r"^https?://")


class WebProcessor:
    """Fetch and extract content from web URLs."""

    async def process(self, source: str, **kwargs: object) -> ProcessedContent:
        from genesis.web.fetch import WebFetcher

        fetcher = WebFetcher()
        result = await fetcher.fetch(source)

        if result.error:
            raise RuntimeError(f"Failed to fetch {source}: {result.error}")

        return ProcessedContent(
            text=result.text,
            metadata={
                "url": result.url,
                "title": result.title,
                "status_code": result.status_code,
                "truncated": result.truncated,
            },
            source_type="web",
            source_path=source,
        )

    def can_handle(self, source: str) -> bool:
        return bool(_URL_PATTERN.match(source))
