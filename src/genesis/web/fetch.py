"""URL fetching with HTML-to-text extraction for LLM consumption."""

from __future__ import annotations

import logging

import httpx

from genesis.security import ContentSanitizer, ContentSource
from genesis.web.types import FetchResult

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 50_000  # ~12k tokens
_DEFAULT_TIMEOUT = 20.0
_USER_AGENT = "Genesis/3.0 (research bot)"


class WebFetcher:
    """Async URL fetcher with HTML-to-text extraction."""

    def __init__(
        self,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT,
        max_chars: int = _DEFAULT_MAX_CHARS,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        )
        self._max_chars = max_chars

    async def fetch(self, url: str, *, max_chars: int | None = None) -> FetchResult:
        """Fetch a URL and return cleaned text. Never raises."""
        limit = max_chars or self._max_chars
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Fetch failed for %s: %s", url, exc)
            status = getattr(exc, "response", None)
            code = status.status_code if status else 0
            return FetchResult(url=url, text="", status_code=code, error=str(exc))

        content_type = resp.headers.get("content-type", "")
        if "text/html" in content_type or "application/xhtml" in content_type:
            try:
                text, title = _extract_html(resp.text)
            except Exception:
                logger.warning("HTML extraction failed for %s — using raw text", url, exc_info=True)
                text, title = resp.text[:5000], ""
        elif "text/plain" in content_type or "application/json" in content_type:
            text, title = resp.text, ""
        else:
            return FetchResult(
                url=url, text="", status_code=resp.status_code,
                error=f"Unsupported content type: {content_type}",
            )

        truncated = len(text) > limit
        if truncated:
            text = text[:limit]

        # Wrap in boundary markers for injection defense
        sanitizer = ContentSanitizer()
        text = sanitizer.wrap_content(text, ContentSource.WEB_FETCH)

        return FetchResult(
            url=url, text=text, title=title,
            status_code=resp.status_code, truncated=truncated,
        )


def _extract_html(html: str) -> tuple[str, str]:
    """Extract readable text and title from HTML."""
    import html2text
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")

    title = soup.title.string.strip() if soup.title and soup.title.string else ""

    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "iframe"]):
        tag.decompose()

    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True
    h.body_width = 0
    text = h.handle(str(soup))

    return text.strip(), title
