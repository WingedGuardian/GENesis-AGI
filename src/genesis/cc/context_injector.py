"""Context injector — inject relevant prior experience into prompts."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from genesis.memory.provenance import is_external, wrap_external_recall
from genesis.security import immunity_shadow

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
        blockable = 0
        for r in results:
            content = r.content[:200]
            # Injection defense (PR2): this path recalls source="episodic",
            # which since #1021 CAN carry stored-external rows (dispatched
            # sessions ingesting external content) — so wrap keys on STORED
            # origin first, with the collection check kept for any future
            # `source` widening into the KB.
            _blockable = immunity_shadow.item_is_blockable(
                collection=getattr(r, "collection", None),
                source_pipeline=getattr(r, "source_pipeline", None),
                origin_class=getattr(r, "origin_class", None),
            )
            if _blockable or is_external(getattr(r, "collection", "")):
                content = wrap_external_recall(
                    content, source_pipeline=getattr(r, "source_pipeline", None),
                )
            if _blockable:
                blockable += 1
            lines.append(
                f"- **[{r.memory_type}]** (score: {r.score:.2f}) {content}"
            )

        # WS-3 B1 gate 4 (injection): shadow-record external content reaching the
        # CC context prompt. Episodic-only today -> 0 rows; wired for future
        # widening (observe-only; db=None -> self-resolve).
        await immunity_shadow.record_would_block(
            gate="injection", source_kind="recall_inject",
            source_ref="cc/context_injector.py::inject", process="server",
            blockable_count=blockable, db=None,
        )
        return "\n".join(lines)
