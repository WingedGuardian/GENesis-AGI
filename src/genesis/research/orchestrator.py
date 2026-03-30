"""ResearchOrchestrator — multi-provider search with deduplication."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from genesis.providers.types import ProviderCategory
from genesis.research.types import ResearchResult, SearchResult

if TYPE_CHECKING:
    from genesis.providers.registry import ProviderRegistry

logger = logging.getLogger("genesis.research.orchestrator")


class ResearchOrchestrator:
    """Fans out queries to multiple SearchProviders, deduplicates by URL."""

    def __init__(
        self,
        registry: ProviderRegistry,
        event_bus=None,
    ) -> None:
        self._registry = registry
        self._event_bus = event_bus

    async def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        providers: list[str] | None = None,
    ) -> ResearchResult:
        """Search across providers. Deduplicates by URL."""
        if providers:
            search_providers = [
                p for p in (self._registry.get(name) for name in providers) if p is not None
            ]
        else:
            search_providers = self._registry.list_by_category(ProviderCategory.SEARCH)

        if not search_providers:
            return ResearchResult(query=query)

        sources_queried = [p.name for p in search_providers]

        # Fan out concurrently
        async def _safe_search(provider) -> list[SearchResult]:
            try:
                if hasattr(provider, "search"):
                    return await provider.search(query, max_results=max_results)
                result = await provider.invoke({"query": query, "max_results": max_results})
                if result.success and isinstance(result.data, list):
                    return result.data
                return []
            except NotImplementedError:
                logger.debug("Provider %s not implemented, skipping", provider.name)
                return []
            except Exception:
                logger.warning(
                    "Provider %s failed during search", provider.name, exc_info=True
                )
                return []

        all_results_lists = await asyncio.gather(
            *[_safe_search(p) for p in search_providers]
        )

        # Flatten and deduplicate by URL
        seen_urls: set[str] = set()
        deduped: list[SearchResult] = []
        total = 0
        for results in all_results_lists:
            for r in results:
                total += 1
                if r.url not in seen_urls:
                    seen_urls.add(r.url)
                    deduped.append(r)

        return ResearchResult(
            query=query,
            results=deduped[:max_results],
            sources_queried=sources_queried,
            deduplicated_count=total - len(deduped),
        )

    async def search_and_synthesize(
        self,
        query: str,
        *,
        max_results: int = 10,
        router=None,
    ) -> ResearchResult:
        """Search then synthesize results via LLM.

        Uses call site ``34_research_synthesis`` to produce a synthesis
        paragraph from search results.  Falls back to raw results when
        no router is provided or the LLM call fails.
        """
        result = await self.search(query, max_results=max_results)

        if not result.results or router is None:
            return result

        # Format search results as context for the LLM
        context_lines: list[str] = []
        for i, sr in enumerate(result.results, 1):
            context_lines.append(f"[{i}] {sr.title}\n    {sr.url}\n    {sr.snippet}")
        context_block = "\n\n".join(context_lines)

        prompt = (
            f"Synthesize the following search results for the query: {query!r}\n\n"
            f"{context_block}\n\n"
            "Provide a concise synthesis paragraph summarizing the key findings."
        )

        try:
            from genesis.perception.caller import LLMCaller

            caller = LLMCaller(router=router)
            llm_response = await caller.call(prompt, call_site_id="34_research_synthesis")
            if llm_response is not None:
                return replace(result, synthesis=llm_response.text)
        except Exception:
            logger.error(
                "LLM synthesis failed for query %r, returning raw results",
                query,
                exc_info=True,
            )

        return result
