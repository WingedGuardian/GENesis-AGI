"""Workaround search stub --- procedural memory only for V3.

Implements the WorkaroundSearcher protocol. Full 5-step protocol
(procedural memory, tool registry, LLM reasoning, web search,
model escalation) deferred to V4 per Amendment #6.

V3 behavior: check procedural memory for similar past fixes,
return the best match if found, otherwise signal not-found.
"""

from __future__ import annotations

import logging
from typing import Any

from genesis.autonomy.executor.types import WorkaroundResult

logger = logging.getLogger(__name__)


class WorkaroundSearcherImpl:
    """V3 stub: procedural memory lookup only."""

    def __init__(self, *, db: Any | None = None) -> None:
        self._db = db

    async def search(
        self,
        step: dict,
        error: str,
        prior_attempts: list[str],
    ) -> WorkaroundResult | None:
        """Search procedural memory for a workaround.

        Returns WorkaroundResult(found=True, approach=...) if a relevant
        procedure is found, or WorkaroundResult(found=False) otherwise.
        Returns None if no DB is available.
        """
        if self._db is None:
            logger.warning("Workaround search skipped: no database")
            return None

        # Build context tags from step metadata + error keywords
        tags = [step.get("type", "code")]
        description = step.get("description", "")
        if description:
            tags.append(description[:50])
        # Extract first meaningful word from error
        error_words = error.split()[:3]
        tags.extend(w.lower() for w in error_words if len(w) > 3)

        try:
            from genesis.learning.procedural.matcher import find_relevant

            matches = await find_relevant(
                self._db,
                context_tags=tags,
                min_confidence=0.3,
                limit=3,
            )
        except Exception:
            logger.error(
                "Procedural memory lookup failed for step %s",
                step.get("idx", "?"),
                exc_info=True,
            )
            return WorkaroundResult(found=False)

        if not matches:
            logger.debug(
                "No procedural matches for step %s (tags: %s)",
                step.get("idx", "?"), tags,
            )
            return WorkaroundResult(found=False)

        best = matches[0]
        logger.info(
            "Found procedural workaround for step %s: %s (confidence: %.2f)",
            step.get("idx", "?"),
            best.task_type,
            best.confidence,
        )
        # ProcedureMatch.steps is list[str] | None
        approach = "\n".join(best.steps) if best.steps else str(best)
        return WorkaroundResult(found=True, approach=approach)
