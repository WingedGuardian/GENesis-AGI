"""Execution trace recording and retrospective storage.

Implements the ExecutionTracerProto protocol from types.py.
Records step results, quality gate outcomes, and finalizes traces
by storing them as episodic memories for cross-session learning.

Post-finalization, runs an LLM-driven retrospective to extract
reusable procedures, update existing procedure confidence, and
flag skill improvements.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any, Protocol

from genesis.autonomy.executor.types import ExecutionTrace, StepResult

logger = logging.getLogger(__name__)

_CALL_SITE = "43_task_retrospective"

_RETROSPECTIVE_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "identity" / "TASK_RETROSPECTIVE.md"
)

# Caps to prevent runaway storage from verbose LLM responses
_MAX_NEW_PROCEDURES = 3
_MAX_PROCEDURE_UPDATES = 5
_MAX_SKILL_OBSERVATIONS = 3


class _Router(Protocol):
    async def route_call(
        self, call_site_id: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> Any: ...


class ExecutionTracer:
    """Records execution traces and stores them as episodic memories."""

    def __init__(
        self,
        *,
        db: Any | None = None,
        memory_store: Any | None = None,
        router: _Router | None = None,
    ) -> None:
        self._db = db
        self._memory_store = memory_store
        self._router = router

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
        except Exception:
            logger.error(
                "Failed to store execution trace for task %s",
                trace.task_id,
                exc_info=True,
            )
            return None

        # Run LLM-driven retrospective for learning extraction
        await self._run_retrospective(trace, summary)

        return summary

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

    # ------------------------------------------------------------------
    # LLM-driven retrospective — extract procedures, update confidence,
    # flag skill improvements
    # ------------------------------------------------------------------

    async def _run_retrospective(
        self,
        trace: ExecutionTrace,
        summary: str,
    ) -> None:
        """Analyze execution trace via LLM and extract learnings.

        Failures create a follow-up for visibility — not silently swallowed.
        """
        if self._router is None:
            logger.info(
                "Retrospective skipped for task %s: no router available",
                trace.task_id,
            )
            return

        # Skip retrospective for trivial traces (single-step fallbacks, all failed)
        completed = sum(1 for s in trace.step_results if s.status == "completed")
        if completed == 0:
            logger.info(
                "Retrospective skipped for task %s: no completed steps",
                trace.task_id,
            )
            return

        try:
            prompt = await self._build_retrospective_prompt(trace, summary)
            result = await self._router.route_call(
                _CALL_SITE,
                [{"role": "user", "content": prompt}],
            )

            if not result.success or not result.content:
                await self._record_retrospective_failure(
                    trace.task_id,
                    f"LLM call failed: success={getattr(result, 'success', None)}",
                )
                return

            data = self._parse_retrospective_response(result.content)
            if data is None:
                await self._record_retrospective_failure(
                    trace.task_id,
                    f"Failed to parse response: {result.content[:200]}",
                )
                return

            await self._act_on_retrospective(trace, data)

        except Exception as exc:
            logger.error(
                "Retrospective failed for task %s",
                trace.task_id,
                exc_info=True,
            )
            await self._record_retrospective_failure(
                trace.task_id, f"Exception: {exc}",
            )

    async def _build_retrospective_prompt(
        self,
        trace: ExecutionTrace,
        summary: str,
    ) -> str:
        """Build the retrospective prompt from template + trace."""
        template = ""
        try:
            template = _RETROSPECTIVE_PROMPT_PATH.read_text(encoding="utf-8")
        except OSError:
            logger.error(
                "Failed to load retrospective prompt from %s",
                _RETROSPECTIVE_PROMPT_PATH,
                exc_info=True,
            )
            # Fallback: minimal prompt without template
            template = (
                "Analyze this task execution trace and extract learnings.\n\n"
                "## Execution Trace\n{{trace_summary}}\n\n"
                "## Existing Procedures\n{{existing_procedures}}\n\n"
                "Return JSON: {\"new_procedures\": [], "
                "\"procedure_updates\": [], \"skill_observations\": []}"
            )

        # Replace placeholders
        prompt = template.replace("{{trace_summary}}", summary)

        # Load existing procedures so LLM can recommend updates
        existing_procs = await self._load_existing_procedures(trace)
        prompt = prompt.replace("{{existing_procedures}}", existing_procs)

        return prompt

    async def _load_existing_procedures(self, trace: ExecutionTrace) -> str:
        """Load relevant procedures for the retrospective prompt context."""
        if self._db is None:
            return "No procedures database available."

        try:
            from genesis.learning.procedural.matcher import find_relevant

            # Extract keywords from user request for procedure matching
            words = [
                w for w in trace.user_request.lower().split()
                if len(w) > 3
            ][:10]

            if not words:
                return "No relevant procedures found."

            matches = await find_relevant(
                self._db, words, min_confidence=0.1, limit=10,
            )
            if not matches:
                return "No relevant procedures found."

            lines = []
            for m in matches:
                lines.append(
                    f"- **{m.task_type}** ({m.confidence:.0%}): {m.principle}"
                )
            return "\n".join(lines)
        except Exception:
            logger.debug("Failed to load existing procedures", exc_info=True)
            return "Failed to load existing procedures."

    def _parse_retrospective_response(self, content: str) -> dict | None:
        """Parse JSON from retrospective LLM response."""
        text = content.strip()

        # Strip markdown code fences
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        with contextlib.suppress(json.JSONDecodeError, ValueError):
            data = json.loads(text)
            if isinstance(data, dict):
                return data

        return None

    async def _act_on_retrospective(
        self,
        trace: ExecutionTrace,
        data: dict,
    ) -> None:
        """Process retrospective extractions: store procedures, update confidence, flag skills."""

        # 1. New procedures
        new_procs = data.get("new_procedures", [])
        if isinstance(new_procs, list):
            for proc_data in new_procs[:_MAX_NEW_PROCEDURES]:
                if not isinstance(proc_data, dict):
                    continue
                try:
                    await self._store_new_procedure(trace, proc_data)
                except Exception:
                    logger.warning(
                        "Failed to store procedure from retrospective",
                        exc_info=True,
                    )

        # 2. Procedure updates
        updates = data.get("procedure_updates", [])
        if isinstance(updates, list):
            for update in updates[:_MAX_PROCEDURE_UPDATES]:
                if not isinstance(update, dict):
                    continue
                try:
                    await self._update_procedure(update)
                except Exception:
                    logger.warning(
                        "Failed to update procedure from retrospective",
                        exc_info=True,
                    )

        # 3. Skill observations
        skill_obs = data.get("skill_observations", [])
        if isinstance(skill_obs, list):
            for obs in skill_obs[:_MAX_SKILL_OBSERVATIONS]:
                if not isinstance(obs, dict):
                    continue
                try:
                    await self._record_skill_observation(obs)
                except Exception:
                    logger.warning(
                        "Failed to record skill observation",
                        exc_info=True,
                    )

        proc_count = len(trace.procedural_extractions)
        if proc_count:
            logger.info(
                "Retrospective for task %s: %d procedures extracted",
                trace.task_id, proc_count,
            )

    async def _store_new_procedure(
        self,
        trace: ExecutionTrace,
        proc_data: dict,
    ) -> None:
        """Store a new procedure extracted from the retrospective."""
        if self._db is None:
            return

        required = ("task_type", "principle", "steps", "tools_used", "context_tags")
        if not all(k in proc_data and proc_data[k] for k in required):
            logger.debug("Skipping procedure with missing fields: %s", list(proc_data.keys()))
            return

        from genesis.learning.procedural.operations import store_procedure

        proc_id = await store_procedure(
            self._db,
            task_type=proc_data["task_type"],
            principle=proc_data["principle"],
            steps=proc_data["steps"],
            tools_used=proc_data["tools_used"],
            context_tags=proc_data["context_tags"],
            activation_tier="L4",
            speculative=1,
        )

        trace.procedural_extractions.append(proc_id)
        logger.info(
            "Stored procedure %s (%s) from task %s retrospective",
            proc_id[:8], proc_data["task_type"], trace.task_id,
        )

    async def _update_procedure(self, update: dict) -> None:
        """Update an existing procedure based on retrospective analysis."""
        if self._db is None:
            return

        task_type = update.get("task_type", "")
        outcome = update.get("outcome", "")
        if not task_type or not outcome:
            return

        # Use find_relevant with task_type as a context tag — find_best_match
        # with empty tags always returns None (Jaccard overlap = 0).
        from genesis.learning.procedural.matcher import find_relevant

        matches = await find_relevant(
            self._db, [task_type], min_confidence=0.0, limit=1,
        )
        if not matches:
            logger.debug("No existing procedure found for type '%s'", task_type)
            return
        match = matches[0]

        from genesis.learning.procedural.operations import (
            record_failure,
            record_success,
            record_workaround,
        )

        if outcome == "success":
            await record_success(self._db, match.procedure_id)
            logger.info(
                "Recorded success for procedure %s (%s)",
                match.procedure_id[:8], task_type,
            )
        elif outcome == "failure":
            condition = update.get("failure_condition", "unknown")
            await record_failure(self._db, match.procedure_id, condition=condition)
            logger.info(
                "Recorded failure for procedure %s (%s): %s",
                match.procedure_id[:8], task_type, condition,
            )

            # Record workaround if provided
            workaround = update.get("workaround")
            if workaround:
                await record_workaround(
                    self._db, match.procedure_id,
                    failed_method=condition,
                    working_method=workaround,
                    context=f"task retrospective: {task_type}",
                )

    async def _record_skill_observation(self, obs: dict) -> None:
        """Record a skill improvement observation."""
        if self._memory_store is None:
            return

        skill_name = obs.get("skill_name", "")
        observation = obs.get("observation", "")
        if not skill_name or not observation:
            return

        await self._memory_store.store(
            content=f"Skill update candidate ({skill_name}): {observation}",
            source="task_retrospective",
            memory_type="episodic",
            tags=["skill_update_candidate", skill_name],
            confidence=0.5,
        )
        logger.info("Recorded skill observation for '%s'", skill_name)

    async def _record_retrospective_failure(
        self,
        task_id: str,
        error_detail: str,
    ) -> None:
        """Create a follow-up when retrospective fails — visible, not silent."""
        logger.warning(
            "Retrospective failed for task %s: %s", task_id, error_detail,
        )
        if self._db is None:
            return

        try:
            from genesis.db.crud import follow_ups as follow_up_crud

            await follow_up_crud.create(
                self._db,
                content=(
                    f"Task {task_id[:12]} retrospective extraction failed: "
                    f"{error_detail[:200]}"
                ),
                source="task_executor",
                strategy="ego_judgment",
                reason="Retrospective learning extraction failed — needs investigation",
                priority="medium",
            )
        except Exception:
            # Last resort — at least the log warning above was emitted
            logger.error(
                "Failed to create follow-up for retrospective failure",
                exc_info=True,
            )
