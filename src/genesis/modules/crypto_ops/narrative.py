"""Narrative detector — consumes pipeline output, ranks emerging narratives."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from genesis.modules.crypto_ops.types import Narrative, NarrativeStatus

logger = logging.getLogger(__name__)

NARRATIVE_PROMPT = """You are analyzing crypto market signals to detect emerging narratives.

Signals (from Knowledge Pipeline):
{signals}

Identify any emerging narrative themes. A narrative is a coherent story or trend
gaining social/market energy (e.g., "AI agents on Solana", "meme coins on Base",
"RWA tokenization wave").

For each narrative found, respond with a JSON array:
[{{"name": "<short name>", "description": "<1-2 sentences>", \
"momentum": <0.0-1.0>, "signals": ["<supporting signal>", ...], \
"categories": ["<category>", ...]}}]

If no clear narratives: []

Rules:
- Only surface narratives with multiple supporting signals
- Score momentum based on signal recency, volume, and convergence
- Distinguish genuine trends from noise / single-source hype
"""


class NarrativeDetector:
    """Detects and ranks emerging crypto narratives from pipeline signals.

    Consumes Knowledge Pipeline output for the crypto-ops research profile.
    Maintains a ranked list of active narratives with momentum scores.
    Answers: "what's gaining energy right now?"
    """

    def __init__(self) -> None:
        self._narratives: dict[str, Narrative] = {}

    @property
    def active_narratives(self) -> list[Narrative]:
        """Return narratives sorted by momentum, excluding faded ones."""
        return sorted(
            [n for n in self._narratives.values() if n.status != NarrativeStatus.FADING],
            key=lambda n: n.momentum_score,
            reverse=True,
        )

    @property
    def narrative_count(self) -> int:
        return len(self._narratives)

    def add_narrative(self, narrative: Narrative) -> None:
        """Add or update a narrative."""
        self._narratives[narrative.id] = narrative

    def get_narrative(self, narrative_id: str) -> Narrative | None:
        return self._narratives.get(narrative_id)

    def update_momentum(self, narrative_id: str, momentum: float) -> None:
        """Update momentum score and adjust status."""
        n = self._narratives.get(narrative_id)
        if n is None:
            return

        n.momentum_score = max(0.0, min(1.0, momentum))
        n.last_updated = time.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Status transitions based on momentum
        if n.momentum_score >= 0.7:
            n.status = NarrativeStatus.PEAKING
        elif n.momentum_score >= 0.4:
            n.status = NarrativeStatus.BUILDING
        elif n.momentum_score >= 0.1:
            n.status = NarrativeStatus.EMERGING
        else:
            n.status = NarrativeStatus.FADING

    async def detect(
        self,
        signals: list[str],
        *,
        router: Any = None,
    ) -> list[Narrative]:
        """Detect narratives from a batch of pipeline signals.

        Args:
            signals: List of signal content strings from Knowledge Pipeline.
            router: LLM router for narrative analysis.

        Returns:
            List of newly detected or updated narratives.
        """
        if not signals or router is None:
            return []

        prompt = NARRATIVE_PROMPT.format(
            signals="\n".join(f"- {s}" for s in signals[:50]),  # Cap at 50
        )

        try:
            response = await router.route(prompt, tier="free")
            detected = json.loads(response)
        except Exception:
            logger.warning("Narrative detection LLM call failed", exc_info=True)
            return []

        if not isinstance(detected, list):
            return []

        new_narratives = []
        for item in detected:
            narrative = Narrative(
                name=item.get("name", ""),
                description=item.get("description", ""),
                momentum_score=float(item.get("momentum", 0.5)),
                signals=item.get("signals", []),
                categories=item.get("categories", []),
            )
            # Set status based on momentum
            if narrative.momentum_score >= 0.7:
                narrative.status = NarrativeStatus.PEAKING
            elif narrative.momentum_score >= 0.4:
                narrative.status = NarrativeStatus.BUILDING

            self._narratives[narrative.id] = narrative
            new_narratives.append(narrative)

        logger.info("Detected %d narratives from %d signals", len(new_narratives), len(signals))
        return new_narratives
