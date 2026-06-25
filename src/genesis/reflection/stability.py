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

# Learning-regression alarm gating. A regression fires only when the
# procedure-effectiveness score declines for N consecutive assessments AND the
# total drop is meaningful AND the latest score is below a healthy floor. This
# keeps noisy LLM-assigned scores from latching a persistent CRITICAL alarm on
# small dips or a high-but-slightly-declining trajectory.
_MIN_REGRESSION_DROP = 0.10
_MAX_HEALTHY_SCORE = 0.60

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

        # Consecutive decline (scores ordered newest-first: scores[0] = newest).
        if not all(scores[i] < scores[i + 1] for i in range(weeks)):
            return False
        # Gate on magnitude AND absolute level: a couple of small dips in noisy
        # LLM-assigned scores, or a high-but-dipping trajectory (e.g.
        # 0.85 -> 0.80 -> 0.75), must not latch a persistent regression alarm.
        total_drop = scores[weeks] - scores[0]  # oldest minus newest; >0 = declined
        return total_drop >= _MIN_REGRESSION_DROP and scores[0] < _MAX_HEALTHY_SCORE

    async def emit_regression_signal(
        self, db: aiosqlite.Connection,
    ) -> None:
        """Emit a learning regression event and update cognitive state."""
        now = datetime.now(UTC).isoformat()

        # Supersede any prior unresolved learning_regression so only the latest
        # standing signal remains and its timestamp reflects this detection —
        # stale regression notes otherwise persist in ego / morning-report
        # context until their TTL.
        await observations.resolve_by_source_and_type(
            db,
            source="stability_monitor",
            type="learning_regression",
            resolved_at=now,
            resolution_notes="superseded by newer regression detection",
        )

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

    async def resolve_regression_if_standing(
        self, db: aiosqlite.Connection,
    ) -> None:
        """Resolve a standing learning_regression once effectiveness recovers.

        Called from the calibration path when ``check_regression`` returns
        False, so a regression alarm clears on recovery instead of lingering in
        ego / morning-report context until its TTL.
        """
        now = datetime.now(UTC).isoformat()
        resolved = await observations.resolve_by_source_and_type(
            db,
            source="stability_monitor",
            type="learning_regression",
            resolved_at=now,
            resolution_notes="procedure effectiveness recovered (no regression this cycle)",
        )
        if resolved:
            logger.info(
                "Resolved %d standing learning_regression observation(s) on recovery",
                resolved,
            )

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
