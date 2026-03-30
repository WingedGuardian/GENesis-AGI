"""Learning stability monitoring — quarantine, regression, contradiction detection."""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.db.crud import observations, procedural

if TYPE_CHECKING:
    import aiosqlite

    from genesis.observability.events import GenesisEventBus

logger = logging.getLogger(__name__)

# Quarantine thresholds
_MIN_USES_FOR_QUARANTINE = 3
_MAX_SUCCESS_RATE_FOR_QUARANTINE = 0.40

# Negation patterns for simple contradiction detection
_NEGATION_PAIRS = [
    ("always", "never"),
    ("should", "should not"),
    ("must", "must not"),
    ("increase", "decrease"),
    ("improve", "degrade"),
    ("success", "failure"),
    ("enable", "disable"),
]


class LearningStabilityMonitor:
    """Monitors learning system health: quarantine, regression, contradictions."""

    def __init__(self, *, event_bus: GenesisEventBus | None = None):
        self._event_bus = event_bus

    async def check_quarantine_candidates(
        self, db: aiosqlite.Connection,
    ) -> list[dict]:
        """Find procedures with 3+ uses and <40% success rate."""
        active = await procedural.list_active(db)
        candidates = []
        for proc in active:
            total = proc["success_count"] + proc["failure_count"]
            if total < _MIN_USES_FOR_QUARANTINE:
                continue
            rate = proc["success_count"] / total
            if rate < _MAX_SUCCESS_RATE_FOR_QUARANTINE:
                candidates.append({
                    "procedure_id": proc["id"],
                    "task_type": proc["task_type"],
                    "success_rate": round(rate, 3),
                    "total_uses": total,
                    "reason": (
                        f"Success rate {rate:.0%} < {_MAX_SUCCESS_RATE_FOR_QUARANTINE:.0%} "
                        f"threshold after {total} uses"
                    ),
                })
        return candidates

    async def execute_quarantine(
        self, db: aiosqlite.Connection, procedure_id: str, reason: str,
    ) -> bool:
        """Quarantine a procedure: set flag, store observation, emit event."""
        success = await procedural.quarantine(db, procedure_id)
        if not success:
            logger.warning("Failed to quarantine procedure %s (not found?)", procedure_id)
            return False

        # Store observation
        now = datetime.now(UTC).isoformat()
        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="stability_monitor",
            type="procedure_quarantined",
            content=json.dumps({
                "procedure_id": procedure_id,
                "reason": reason,
            }),
            priority="high",
            created_at=now,
            skip_if_duplicate=True,
        )

        # Emit event
        if self._event_bus:
            from genesis.observability.types import Severity, Subsystem
            await self._event_bus.emit(
                Subsystem.LEARNING, Severity.WARNING,
                "procedure.quarantined",
                f"Procedure {procedure_id} quarantined: {reason}",
                procedure_id=procedure_id,
            )

        logger.info("Quarantined procedure %s: %s", procedure_id, reason)
        return True

    async def check_regression(
        self, db: aiosqlite.Connection, *, weeks: int = 2,
    ) -> bool:
        """Check if procedure effectiveness has declined for N consecutive weeks.

        Compares successive weekly self-assessment scores. Returns True if
        the procedure_effectiveness dimension has declined for `weeks`
        consecutive assessments.
        """
        assessments = await observations.query(
            db, type="self_assessment", limit=weeks + 1,
        )
        if len(assessments) < weeks + 1:
            return False  # Not enough data

        # Extract procedure_effectiveness scores
        scores = []
        for a in assessments[:weeks + 1]:
            try:
                data = json.loads(a["content"])
                for dim in data.get("dimensions", []):
                    if dim.get("dimension") == "procedure_effectiveness":
                        scores.append(dim.get("score", 0.0))
                        break
            except (json.JSONDecodeError, TypeError, KeyError):
                continue

        if len(scores) < weeks + 1:
            return False

        # Check consecutive decline (scores are ordered newest first)
        return all(scores[i] < scores[i + 1] for i in range(weeks))

    async def emit_regression_signal(
        self, db: aiosqlite.Connection,
    ) -> None:
        """Emit a learning regression event and update cognitive state."""
        now = datetime.now(UTC).isoformat()

        # Write observation instead of overwriting state_flags.
        # State flags are now computed from real data; regressions surface
        # through the observation pipeline where they can be retrieved and
        # acted upon by reflection cycles.
        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="stability_monitor",
            type="learning_regression",
            content="LEARNING REGRESSION DETECTED: Procedure effectiveness has declined "
                    "for 2+ consecutive weeks. Review procedures and quarantine candidates.",
            priority="high",
            created_at=now,
            skip_if_duplicate=True,
        )

        if self._event_bus:
            from genesis.observability.types import Severity, Subsystem
            await self._event_bus.emit(
                Subsystem.LEARNING, Severity.WARNING,
                "learning.regression",
                "Procedure effectiveness declining for 2+ consecutive weeks",
            )

        logger.warning("Learning regression signal emitted")

    def find_contradictions(
        self, obs_list: list[dict],
    ) -> list[tuple[str, str, str]]:
        """Find pairs of potentially contradictory observations.

        Uses simple keyword/negation heuristics. Returns list of
        (obs_id_a, obs_id_b, nature) tuples. Actual resolution is
        LLM-driven in the deep reflection prompt.
        """
        results = []
        for i, obs_a in enumerate(obs_list):
            content_a = (obs_a.get("content") or "").lower()
            id_a = obs_a.get("id", f"obs_{i}")
            for j, obs_b in enumerate(obs_list[i + 1:], start=i + 1):
                content_b = (obs_b.get("content") or "").lower()
                id_b = obs_b.get("id", f"obs_{j}")

                for pos, neg in _NEGATION_PAIRS:
                    if (pos in content_a and neg in content_b) or \
                       (neg in content_a and pos in content_b):
                        # Check they're about the same topic (share 2+ non-trivial words)
                        words_a = set(re.findall(r'\b\w{4,}\b', content_a))
                        words_b = set(re.findall(r'\b\w{4,}\b', content_b))
                        shared = words_a & words_b - {"should", "always", "never", "must", "that", "this", "with"}
                        if len(shared) >= 2:
                            results.append((
                                id_a, id_b,
                                f"Potential contradiction via '{pos}/{neg}' "
                                f"on shared topics: {', '.join(list(shared)[:3])}",
                            ))
                            break  # One contradiction pair per obs pair is enough

        return results
