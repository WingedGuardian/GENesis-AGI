"""SearchProvider — specialized ToolProvider for search."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from genesis.providers.protocol import ToolProvider
from genesis.providers.types import (
    ProviderResult,
)
from genesis.research.types import SearchResult


@runtime_checkable
class SearchProvider(ToolProvider, Protocol):
    """A ToolProvider specialized for web search."""

    async def search(self, query: str, *, max_results: int = 10) -> list[SearchResult]: ...

    async def invoke(self, request: dict) -> ProviderResult:
        """Default invoke delegates to search."""
        query = request.get("query", request.get("q", ""))
        max_results = request.get("max_results", 10)
        results = await self.search(query, max_results=max_results)
        return ProviderResult(
            success=bool(results),
            data=results,
            provider_name=self.name,
        )
