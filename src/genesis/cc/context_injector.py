"""Context injector — inject relevant prior experience into prompts."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from genesis.memory.provenance import is_external, wrap_external_recall

if TYPE_CHECKING:
    from genesis.memory.retrieval import HybridRetriever

logger = logging.getLogger(__name__)


class ContextInjector:
    """Injects relevant memories from prior runs into prompts."""

    def __init__(self, *, retriever: HybridRetriever | None = None):
        self._retriever = retriever

    def set_retriever(self, retriever: HybridRetriever) -> None:
        self._retriever = retriever

    async def inject(
        self,
        task_description: str,
        *,
        limit: int = 5,
    ) -> str:
        """Query HybridRetriever and format as context section.

        Returns a markdown section with relevant prior experience,
        or empty string if no retriever or no results.
        """
        if not self._retriever:
            return ""

        try:
            results = await self._retriever.recall(
                task_description,
                source="episodic",
                limit=limit,
            )
        except Exception as exc:
            logger.warning(
                "Context injection retrieval failed: %s: %s",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return ""

        if not results:
            return ""

        lines = ["## Relevant Prior Experience\n"]
        for r in results:
            content = r.content[:200]
            # Injection defense (PR2): this path recalls source="episodic"
            # (first-party) today, so the guard never fires — but wrap defensively
            # so a future `source` widening can't silently inject unwrapped KB.
            if is_external(getattr(r, "collection", "")):
                content = wrap_external_recall(
                    content, source_pipeline=getattr(r, "source_pipeline", None),
                )
            lines.append(
                f"- **[{r.memory_type}]** (score: {r.score:.2f}) {content}"
            )

        return "\n".join(lines)
