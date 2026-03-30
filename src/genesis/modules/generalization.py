"""Generalization filter — quality gate between module and core learning."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Prompt for the generalization filter LLM call
GENERALIZATION_PROMPT = """You are evaluating an outcome from the {module_name} capability module.

Outcome details:
{outcome_details}

Is there a lesson here that would help Genesis reason better in ANY domain?

Rules:
- Domain-specific patterns are NOT generalizable (e.g., market-specific trends, price patterns)
- Process/methodology improvements ARE generalizable (e.g., "breaking research into sub-questions improved quality")
- Source/tool reliability findings ARE generalizable (e.g., "this API returns inconsistent data")
- Reasoning calibration findings ARE generalizable (e.g., "systematically overconfident by 5%")
- Noise and random outcomes are NEVER generalizable
- When in doubt, DO NOT promote. False negatives are safer than false positives.

If generalizable, respond with a JSON object:
{{"generalizable": true, "lesson": "<clear, domain-agnostic observation>", "category": "<process|source_reliability|calibration|tool_effectiveness>"}}

If not generalizable, respond:
{{"generalizable": false, "reason": "<brief explanation>"}}
"""


class GeneralizationFilter:
    """Filters module outcomes for lessons generalizable to Genesis core.

    This is the critical boundary between "hands" (module-specific) and
    "brain" (Genesis core). Only process/methodology/calibration lessons
    cross this boundary. Domain-specific patterns stay isolated.
    """

    def __init__(self, *, observation_writer: Any = None) -> None:
        self._observation_writer = observation_writer

    async def evaluate(
        self,
        module_name: str,
        outcome: dict,
        *,
        router: Any = None,
    ) -> dict | None:
        """Evaluate an outcome for generalizable lessons.

        Args:
            module_name: Name of the source module.
            outcome: The outcome data to evaluate.
            router: Optional LLM router for intelligent evaluation.

        Returns:
            Dict with lesson details if generalizable, None otherwise.
        """
        if router is None:
            # Without an LLM, we can't evaluate generalizability.
            # Conservative: don't promote anything.
            logger.debug("No router available for generalization filter, skipping")
            return None

        prompt = GENERALIZATION_PROMPT.format(
            module_name=module_name,
            outcome_details=str(outcome),
        )

        try:
            response = await router.route(prompt, tier="free")
            result = json.loads(response)
        except Exception:
            logger.warning("Generalization filter LLM call failed", exc_info=True)
            return None

        if not result.get("generalizable", False):
            return None

        return {
            "source": f"module:{module_name}",
            "lesson": result.get("lesson", ""),
            "category": result.get("category", "process"),
        }

    async def promote_to_core(
        self,
        lesson: dict,
        *,
        db: Any = None,
    ) -> str | None:
        """Write a generalizable lesson to Genesis core memory.

        Promoted observations get:
        - source: "module:<name>" (traceable provenance)
        - category: "generalizable_lesson"
        - confidence: low (0.4) — module-derived, needs confirmation
        - speculative: True — until validated by Genesis's own experience
        """
        if self._observation_writer is None or db is None:
            logger.warning("Cannot promote lesson: no observation writer or db")
            return None

        try:
            obs_id = await self._observation_writer.write(
                db,
                source=lesson["source"],
                type="generalizable_lesson",
                content=lesson["lesson"],
                priority="low",
                category=lesson.get("category", "process"),
            )
            logger.info(
                "Promoted generalizable lesson from %s: %s (obs_id=%s)",
                lesson["source"],
                lesson["lesson"][:80],
                obs_id,
            )
            return obs_id
        except Exception:
            logger.warning("Failed to promote lesson to core", exc_info=True)
            return None
