"""Web infrastructure type definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SearchBackend(StrEnum):
    SEARXNG = "searxng"
    BRAVE = "brave"


@dataclass(frozen=True)
class SearchResult:
    """A single web search result, normalized across backends."""

    title: str
    url: str
    snippet: str  # SearXNG "content" / Brave "description"
    backend: SearchBackend
    score: float = 0.0


@dataclass(frozen=True)
class SearchResponse:
    """Full search response with metadata."""

    query: str
    results: list[SearchResult] = field(default_factory=list)
    # Set only by the backend that produced ``results``; None when every
    # backend failed (a default here used to label total failures "searxng").
    backend_used: SearchBackend | None = None
    fallback_used: bool = False
    error: str | None = None


@dataclass(frozen=True)
class FetchResult:
    """Result of fetching and extracting text from a URL."""

    url: str
    text: str
    title: str = ""
    status_code: int = 0
    error: str | None = None
    truncated: bool = False
