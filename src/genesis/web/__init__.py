"""Web search and URL fetching infrastructure for Genesis."""

from genesis.web.fetch import WebFetcher
from genesis.web.search import WebSearcher
from genesis.web.types import FetchResult, SearchBackend, SearchResponse, SearchResult

__all__ = [
    "WebSearcher",
    "WebFetcher",
    "SearchResult",
    "SearchResponse",
    "FetchResult",
    "SearchBackend",
    "search",
    "fetch",
]

_searcher: WebSearcher | None = None
_fetcher: WebFetcher | None = None


def _get_searcher() -> WebSearcher:
    global _searcher
    if _searcher is None:
        _searcher = WebSearcher()
    return _searcher


def _get_fetcher() -> WebFetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = WebFetcher()
    return _fetcher


async def search(query: str, *, max_results: int | None = None) -> SearchResponse:
    """Search the web (SearXNG primary, Brave fallback)."""
    return await _get_searcher().search(query, max_results=max_results)


async def fetch(url: str, *, max_chars: int | None = None) -> FetchResult:
    """Fetch a URL and return cleaned text content."""
    return await _get_fetcher().fetch(url, max_chars=max_chars)
