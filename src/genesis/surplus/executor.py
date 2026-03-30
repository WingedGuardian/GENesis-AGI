"""Surplus executors — stub and reflection-based implementations."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.awareness.types import Depth, DepthScore, SignalReading, TickResult
from genesis.db.crud import observations
from genesis.surplus.types import ExecutorResult, SurplusTask, TaskType

if TYPE_CHECKING:
    import aiosqlite

    from genesis.perception.engine import ReflectionEngine

logger = logging.getLogger(__name__)


class StubExecutor:
    """Stub executor — generates structured placeholders.

    Used as fallback when the ReflectionEngine is unavailable.
    """

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        """Generate a placeholder insight for the given surplus task."""
        content = (
            f"[Stub] Surplus task {task.task_type} completed. "
            f"Drive: {task.drive_alignment}. "
            f"No LLM executor available — using placeholder."
        )
        insight = {
            "content": content,
            "source_task_type": task.task_type,
            "generating_model": "stub",
            "drive_alignment": task.drive_alignment,
            "confidence": 0.0,
        }
        return ExecutorResult(
            success=True,
            content=content,
            insights=[insight],
        )


# Task type → reflection depth mapping
_DEPTH_MAP: dict[TaskType, Depth] = {
    TaskType.BRAINSTORM_USER: Depth.LIGHT,
    TaskType.BRAINSTORM_SELF: Depth.LIGHT,
    TaskType.META_BRAINSTORM: Depth.LIGHT,
    TaskType.MEMORY_AUDIT: Depth.MICRO,
    TaskType.PROCEDURE_AUDIT: Depth.MICRO,
    TaskType.GAP_CLUSTERING: Depth.LIGHT,
    TaskType.SELF_UNBLOCK: Depth.LIGHT,
    TaskType.ANTICIPATORY_RESEARCH: Depth.LIGHT,
    TaskType.PROMPT_EFFECTIVENESS_REVIEW: Depth.MICRO,
    TaskType.CODE_AUDIT: Depth.LIGHT,
    TaskType.INFRASTRUCTURE_MONITOR: Depth.LIGHT,
}


def _synthetic_tick(task: SurplusTask) -> TickResult:
    """Build a minimal TickResult for the ReflectionEngine from a surplus task."""
    now = datetime.now(UTC).isoformat()
    depth = _DEPTH_MAP.get(task.task_type, Depth.LIGHT)
    return TickResult(
        tick_id=f"surplus-{uuid.uuid4().hex[:8]}",
        timestamp=now,
        source="surplus",
        signals=[
            SignalReading(
                name="surplus_task",
                value=task.priority,
                source="surplus_scheduler",
                collected_at=now,
            ),
        ],
        scores=[
            DepthScore(
                depth=depth,
                raw_score=task.priority,
                time_multiplier=1.0,
                final_score=task.priority,
                threshold=0.0,
                triggered=True,
            ),
        ],
        classified_depth=depth,
        trigger_reason=f"surplus:{task.task_type}:{task.drive_alignment}",
    )


def _extract_content(result) -> str:
    """Extract text content from a ReflectionResult's output."""
    output = result.output
    if output is None:
        return result.reason or "Reflection produced no output."
    # LightOutput has .assessment + .recommendations
    if hasattr(output, "assessment"):
        parts = [output.assessment]
        if hasattr(output, "recommendations") and output.recommendations:
            recs = [r if isinstance(r, str) else r.get("text", str(r)) for r in output.recommendations]
            parts.append("Recommendations: " + "; ".join(recs))
        if hasattr(output, "patterns") and output.patterns:
            pats = [p if isinstance(p, str) else p.get("text", str(p)) for p in output.patterns]
            parts.append("Patterns: " + "; ".join(pats))
        return "\n".join(parts)
    # MicroOutput has .summary
    if hasattr(output, "summary"):
        return output.summary
    return str(output)


class ReflectionBasedSurplusExecutor:
    """Executes surplus tasks via the ReflectionEngine (micro/light depth)."""

    def __init__(self, engine: ReflectionEngine, *, db: aiosqlite.Connection) -> None:
        self._engine = engine
        self._db = db

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        depth = _DEPTH_MAP.get(task.task_type, Depth.LIGHT)
        tick = _synthetic_tick(task)

        # Query past findings for deduplication context (Light+ only —
        # Micro templates don't render prior_context)
        prior_context = None
        prior_obs_ids: list[str] = []
        if depth != Depth.MICRO:
            try:
                past_findings = await observations.query(
                    self._db, type=task.task_type, resolved=False, limit=10,
                )
                if past_findings:
                    prior_obs_ids = [o["id"] for o in past_findings if o.get("id")]
                    lines = []
                    for obs in past_findings:
                        lines.append(
                            f"- [{obs.get('priority', '?')}] {obs.get('created_at', '?')}: "
                            f"{obs.get('content', '')}"
                        )
                    prior_context = (
                        "Previously identified findings for this task type "
                        "(avoid re-discovering these):\n" + "\n".join(lines)
                    )
            except Exception:
                logger.warning(
                    "Failed to query prior findings for surplus dedup", exc_info=True,
                )

        try:
            result = await self._engine.reflect(
                depth, tick, db=self._db, prior_context=prior_context,
            )
        except Exception as exc:
            logger.error(
                "Surplus reflection failed for %s: %s", task.task_type, exc, exc_info=True,
            )
            return ExecutorResult(success=False, error=f"{type(exc).__name__}: {exc}")

        if not result.success:
            return ExecutorResult(success=False, error=result.reason)

        content = _extract_content(result)
        confidence = getattr(result.output, "confidence", 0.5) if result.output else 0.3
        model = "reflection_engine"

        insights = [{
            "content": content,
            "source_task_type": task.task_type,
            "generating_model": model,
            "drive_alignment": task.drive_alignment,
            "confidence": confidence,
        }]

        # Mark prior findings as having influenced this surplus execution
        if prior_obs_ids:
            try:
                await observations.mark_influenced_batch(self._db, prior_obs_ids)
            except Exception:
                logger.warning("Failed to mark prior findings as influenced", exc_info=True)

        return ExecutorResult(success=True, content=content, insights=insights)
