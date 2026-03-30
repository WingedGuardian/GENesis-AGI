"""Execution trace recording and retrospective storage.

Implements the ExecutionTracerProto protocol from types.py.
Records step results, quality gate outcomes, and finalizes traces
by storing them as episodic memories for cross-session learning.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from genesis.autonomy.executor.types import ExecutionTrace, StepResult

logger = logging.getLogger(__name__)


class ExecutionTracer:
    """Records execution traces and stores them as episodic memories."""

    def __init__(
        self,
        *,
        db: Any | None = None,
        memory_store: Any | None = None,
    ) -> None:
        self._db = db
        self._memory_store = memory_store

    def start_trace(
        self,
        task_id: str,
        initiated_by: str,
        user_request: str,
    ) -> ExecutionTrace:
        """Create a new execution trace."""
        return ExecutionTrace(
            task_id=task_id,
            initiated_by=initiated_by,
            user_request=user_request,
        )

    def record_step(
        self,
        trace: ExecutionTrace,
        step_result: StepResult,
    ) -> None:
        """Append a step result to the trace and update cost."""
        trace.step_results.append(step_result)
        trace.total_cost_usd += step_result.cost_usd

    def record_quality_gate(
        self,
        trace: ExecutionTrace,
        gate_result: dict,
    ) -> None:
        """Record a quality gate outcome on the trace."""
        trace.quality_gate = gate_result

    async def finalize(self, trace: ExecutionTrace) -> str | None:
        """Store trace as episodic memory. Returns a summary or None on failure."""
        if self._memory_store is None:
            logger.warning(
                "Trace finalize skipped for task %s: no memory store",
                trace.task_id,
            )
            return None

        summary = self._build_summary(trace)
        tags = [
            f"task:{trace.task_id}",
            f"initiated_by:{trace.initiated_by}",
            f"steps:{len(trace.step_results)}",
        ]

        # Classify outcome
        completed_steps = sum(
            1 for s in trace.step_results if s.status == "completed"
        )
        total_steps = len(trace.step_results)
        outcome = "success" if completed_steps == total_steps else "partial"
        tags.append(f"outcome:{outcome}")

        try:
            memory_id = await self._memory_store.store(
                content=summary,
                source="task_executor",
                memory_type="episodic",
                tags=tags,
                confidence=0.7,
            )
            logger.info(
                "Stored execution trace for task %s as memory %s",
                trace.task_id, memory_id,
            )
            trace.retrospective_id = memory_id
            return summary
        except Exception:
            logger.error(
                "Failed to store execution trace for task %s",
                trace.task_id,
                exc_info=True,
            )
            return None

    def _build_summary(self, trace: ExecutionTrace) -> str:
        """Build a human-readable trace summary for episodic storage."""
        parts = [
            f"# Task Execution Trace: {trace.task_id}",
            f"Initiated by: {trace.initiated_by}",
            f"Request: {trace.user_request[:500]}",
            f"Total cost: ${trace.total_cost_usd:.4f}",
            "",
            "## Steps",
        ]

        for step in trace.step_results:
            status_icon = "+" if step.status == "completed" else "-"
            parts.append(
                f"  {status_icon} Step {step.idx}: {step.status} "
                f"({step.duration_s:.1f}s, ${step.cost_usd:.4f})"
            )
            if step.result:
                parts.append(f"    Result: {step.result[:200]}")

        if trace.quality_gate:
            parts.extend(["", "## Quality Gate"])
            parts.append(json.dumps(trace.quality_gate, indent=2))

        if trace.request_delivery_delta:
            parts.extend(["", "## Request-Delivery Delta"])
            parts.append(json.dumps(trace.request_delivery_delta, indent=2))

        return "\n".join(parts)
