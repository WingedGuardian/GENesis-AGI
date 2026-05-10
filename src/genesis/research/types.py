"""Research type definitions."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SearchResult:
    """A single search result, normalized across providers."""

    title: str
    url: str
    snippet: str
    source: str  # provider name (e.g. "searxng", "brave", "perplexity")
    score: float = 0.0


@dataclass(frozen=True)
class ResearchResult:
    """Aggregated result from one or more search providers."""

    query: str
    results: list[SearchResult] = field(default_factory=list)
    sources_queried: list[str] = field(default_factory=list)
    deduplicated_count: int = 0
    synthesis: str | None = None
