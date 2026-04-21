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

# Scrapling gives TLS fingerprint impersonation via curl_cffi —
# requests appear as real Chrome, bypassing most anti-bot detection.
try:
    from scrapling.fetchers import AsyncFetcher as _ScraplingFetcher

    _HAS_SCRAPLING = True
except ImportError:
    _ScraplingFetcher = None  # type: ignore[misc,assignment]
    _HAS_SCRAPLING = False


class WebFetcher:
    """Async URL fetcher with HTML-to-text extraction.

    Uses Scrapling (curl_cffi) when available for TLS fingerprint
    impersonation, falling back to plain httpx otherwise.
    """

    def __init__(
        self,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT,
        max_chars: int = _DEFAULT_MAX_CHARS,
    ) -> None:
        self._timeout_s = timeout_s
        self._client: httpx.AsyncClient | None = None
        self._max_chars = max_chars

    def _get_httpx_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_s,
                follow_redirects=True,
                headers={"User-Agent": _USER_AGENT},
            )
        return self._client

    async def fetch(self, url: str, *, max_chars: int | None = None) -> FetchResult:
        """Fetch a URL and return cleaned text. Never raises."""
        limit = max_chars or self._max_chars

        if _HAS_SCRAPLING:
            body, status_code, content_type, error = await self._fetch_scrapling(url)
        else:
            body, status_code, content_type, error = await self._fetch_httpx(url)

        if error:
            return FetchResult(url=url, text="", status_code=status_code, error=error)

        is_html = (
            "text/html" in content_type
            or "application/xhtml" in content_type
            or body.lstrip()[:15].lower().startswith(("<!doctype", "<html"))
        )
        if is_html:
            try:
                text, title = _extract_html(body)
            except Exception:
                logger.warning("HTML extraction failed for %s — using raw text", url, exc_info=True)
                text, title = body[:5000], ""
        elif "text/plain" in content_type or "application/json" in content_type or not content_type:
            text, title = body, ""
        else:
            return FetchResult(
                url=url, text="", status_code=status_code,
                error=f"Unsupported content type: {content_type}",
            )

        truncated = len(text) > limit
        if truncated:
            text = text[:limit]

        sanitizer = ContentSanitizer()
        text = sanitizer.wrap_content(text, ContentSource.WEB_FETCH)

        return FetchResult(
            url=url, text=text, title=title,
            status_code=status_code, truncated=truncated,
        )

    async def _fetch_scrapling(self, url: str) -> tuple[str, int, str, str | None]:
        """Fetch via Scrapling with TLS fingerprint impersonation.

        Returns (body, status_code, content_type, error).
        """
        try:
            resp = await _ScraplingFetcher.get(
                url, impersonate="chrome", timeout=self._timeout_s,
            )
        except Exception as exc:
            logger.warning("Scrapling fetch failed for %s: %s", url, exc)
            return "", 0, "", str(exc)

        if resp.status >= 400:
            error = f"HTTP {resp.status}"
            logger.warning("Fetch failed for %s: %s", url, error)
            return "", resp.status, "", error

        content_type = resp.headers.get("content-type", "")
        body = resp.body.decode("utf-8", errors="replace")
        return body, resp.status, content_type, None

    async def _fetch_httpx(self, url: str) -> tuple[str, int, str, str | None]:
        """Fetch via plain httpx (fallback when Scrapling not installed).

        Returns (body, status_code, content_type, error).
        """
        client = self._get_httpx_client()
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Fetch failed for %s: %s", url, exc)
            status = getattr(exc, "response", None)
            code = status.status_code if status else 0
            return "", code, "", str(exc)
        content_type = resp.headers.get("content-type", "")
        return resp.text, resp.status_code, content_type, None


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
