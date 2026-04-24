"""Surplus executors — stub and LLM-based implementations.

Surplus tasks use the Router directly for LLM calls.  They produce
surplus-specific insights (not reflection observations).  The reflection
engine is used ONLY by the awareness loop — surplus and reflection are
separate pipelines.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from genesis.db.crud import observations
from genesis.surplus.types import ExecutorResult, SurplusTask, TaskType

if TYPE_CHECKING:
    import aiosqlite

    from genesis.routing.router import Router

logger = logging.getLogger(__name__)


class StubExecutor:
    """Stub executor — generates structured placeholders.

    Used as fallback when the Router is unavailable.
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


# Call site mapping for surplus tasks.  Tasks with their own call sites
# get routed to dedicated provider chains; others use the generic surplus
# analysis call site.
_CALL_SITES: dict[TaskType, str] = {
    TaskType.BRAINSTORM_USER: "12_surplus_brainstorm",
    TaskType.BRAINSTORM_SELF: "12_surplus_brainstorm",
    TaskType.META_BRAINSTORM: "12_surplus_brainstorm",
    TaskType.INFRASTRUCTURE_MONITOR: "37_infrastructure_monitor",
}

# Fallback call site for analytical tasks without a dedicated entry.
_DEFAULT_CALL_SITE = "12_surplus_brainstorm"


# ── Task-specific prompt templates ──────────────────────────────────

_TASK_PROMPTS: dict[TaskType, str] = {
    TaskType.INFRASTRUCTURE_MONITOR: (
        "You are monitoring infrastructure for an autonomous AI system.\n\n"
        "## Recent Signal Data\n{signals}\n\n"
        "## Task\n"
        "Assess the current infrastructure state.  Report ONLY if something "
        "needs attention — resource pressure, degraded services, anomalous "
        "patterns, or trends that could become problems.\n\n"
        "If everything is operating normally with no concerns, respond with "
        "exactly the word NOMINAL and nothing else.\n\n"
        "Respond in plain text (2-4 sentences for concerns, or just NOMINAL)."
    ),
    TaskType.BRAINSTORM_USER: (
        "You are brainstorming ways to create value for your user.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Generate 2-3 concrete, actionable ideas for how the system could "
        "better serve the user based on recent activity and known interests.  "
        "Each idea should be specific enough to act on.\n\n"
        "Respond in plain text with numbered ideas."
    ),
    TaskType.BRAINSTORM_SELF: (
        "You are brainstorming ways to improve your own capabilities.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Identify 2-3 concrete improvements to your own processes, skills, "
        "or knowledge that would make you more effective.  Focus on gaps "
        "exposed by recent work.\n\n"
        "Respond in plain text with numbered ideas."
    ),
    TaskType.META_BRAINSTORM: (
        "You are reviewing the quality of recent brainstorm outputs.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Assess whether recent brainstorms have been useful or repetitive.  "
        "Suggest adjustments to brainstorm focus areas.\n\n"
        "Respond in plain text (2-4 sentences)."
    ),
    TaskType.MEMORY_AUDIT: (
        "You are auditing the memory system for an autonomous AI.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Identify memory quality issues: contradictions, stale entries, "
        "duplicate information, or gaps.  Suggest specific cleanup actions.\n\n"
        "Respond in plain text with bullet points."
    ),
    TaskType.PROCEDURE_AUDIT: (
        "You are auditing learned procedures for an autonomous AI.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Review recent procedures for accuracy and relevance.  Flag any "
        "that are outdated, low-confidence, or contradictory.\n\n"
        "Respond in plain text with bullet points."
    ),
    TaskType.GAP_CLUSTERING: (
        "You are analyzing observation patterns for an autonomous AI.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Cluster recent unresolved observations into themes.  Identify "
        "recurring patterns that suggest systemic issues rather than "
        "one-off events.\n\n"
        "Respond in plain text with grouped findings."
    ),
    TaskType.SELF_UNBLOCK: (
        "You are helping an autonomous AI system get unstuck.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Identify what's blocking progress and suggest concrete unblocking "
        "actions.  Focus on the highest-leverage intervention.\n\n"
        "Respond in plain text (2-4 sentences)."
    ),
    TaskType.ANTICIPATORY_RESEARCH: (
        "You are doing anticipatory research for an autonomous AI.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Based on recent activity patterns, identify topics or capabilities "
        "that the user or system will likely need soon.  Suggest specific "
        "research directions.\n\n"
        "Respond in plain text with numbered suggestions."
    ),
    TaskType.PROMPT_EFFECTIVENESS_REVIEW: (
        "You are reviewing prompt effectiveness for an autonomous AI.\n\n"
        "{context}\n\n"
        "## Task\n"
        "Assess whether recent LLM outputs have been high-quality and "
        "on-target.  Identify prompts or call sites that consistently "
        "produce poor results and suggest improvements.\n\n"
        "Respond in plain text with bullet points."
    ),
}


class SurplusLLMExecutor:
    """Executes surplus analytical tasks via direct Router calls.

    Surplus tasks produce surplus insights (stored in surplus_staging),
    NOT reflection observations.  The reflection engine is not involved.
    """

    def __init__(self, router: Router, *, db: aiosqlite.Connection) -> None:
        self._router = router
        self._db = db
        self._topic_manager = None

    def set_topic_manager(self, manager) -> None:
        """Set TopicManager for posting surplus insights to Telegram."""
        self._topic_manager = manager

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        prompt = await self._build_prompt(task)
        call_site = _CALL_SITES.get(task.task_type, _DEFAULT_CALL_SITE)

        try:
            result = await self._router.route_call(
                call_site,
                [{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            logger.error(
                "Surplus LLM call failed for %s: %s", task.task_type, exc, exc_info=True,
            )
            return ExecutorResult(success=False, error=f"{type(exc).__name__}: {exc}")

        if not result.success or not result.content:
            error = result.error or "LLM returned empty response"
            return ExecutorResult(success=False, error=error)

        content = result.content.strip()

        # Quality gate: NOMINAL means nothing noteworthy — skip insight + Telegram
        if content.upper().startswith("NOMINAL") and len(content) < 40:
            logger.info("Surplus task %s reported NOMINAL — skipping insight", task.task_type)
            return ExecutorResult(success=True, content="", insights=[])

        # Post to Telegram surplus topic
        if self._topic_manager and content:
            await self._post_to_telegram(task, content)

        model = result.model_id or result.provider_used or "unknown"
        return ExecutorResult(
            success=True,
            content=content,
            insights=[{
                "content": content,
                "source_task_type": task.task_type,
                "generating_model": model,
                "drive_alignment": task.drive_alignment,
                "confidence": 0.5,
            }],
        )

    async def _build_prompt(self, task: SurplusTask) -> str:
        """Build task-specific prompt with relevant context."""
        template = _TASK_PROMPTS.get(task.task_type)
        if template is None:
            # Generic fallback for unmapped task types
            return (
                f"You are performing a '{task.task_type}' analysis for an "
                f"autonomous AI system.\n\n"
                f"Provide a brief, actionable assessment.  "
                f"Respond in plain text (2-4 sentences)."
            )

        context = await self._gather_context(task)

        if task.task_type == TaskType.INFRASTRUCTURE_MONITOR:
            return template.format(signals=context)
        return template.format(context=context)

    async def _gather_context(self, task: SurplusTask) -> str:
        """Gather relevant context for the task type."""
        parts: list[str] = []

        # For infrastructure monitor: get latest signals
        if task.task_type == TaskType.INFRASTRUCTURE_MONITOR:
            try:
                from genesis.db.crud import awareness_ticks
                last_tick = await awareness_ticks.last_tick(self._db)
                if last_tick and last_tick.get("signals_json"):
                    signals = json.loads(last_tick["signals_json"])
                    for s in signals:
                        name = s.get("name", "?")
                        value = s.get("value", "?")
                        parts.append(f"- {name}: {value}")
            except Exception:
                parts.append("(Signal data unavailable)")
            return "\n".join(parts) if parts else "(No recent signals)"

        # For analytical tasks: recent observations + basic stats
        try:
            recent_obs = await observations.query(
                self._db, resolved=False, limit=10,
            )
            if recent_obs:
                parts.append("## Recent Unresolved Observations")
                for obs in recent_obs[:7]:
                    content = obs.get("content", "")[:200]
                    parts.append(
                        f"- [{obs.get('type', '?')}] {obs.get('created_at', '?')}: {content}"
                    )
        except Exception:
            pass

        # Prior findings for this specific task type (dedup context)
        try:
            past_findings = await observations.query(
                self._db, type=task.task_type, resolved=False, limit=5,
            )
            if past_findings:
                parts.append("\n## Previous Findings (avoid re-discovering)")
                for obs in past_findings:
                    parts.append(f"- {obs.get('content', '')[:150]}")
        except Exception:
            pass

        return "\n".join(parts) if parts else "(No additional context available)"

    async def _post_to_telegram(self, task: SurplusTask, content: str) -> None:
        """Post surplus insight to the surplus Telegram topic."""
        try:
            from html import escape

            label = task.task_type.replace("_", " ").title()
            text = (
                f"<b>Surplus: {escape(label)}</b>\n\n"
                f"{escape(content[:2000])}"
            )
            await self._topic_manager.send_to_category("surplus", text)
            logger.info("Posted surplus insight to Telegram (task=%s)", task.task_type)
        except Exception:
            logger.warning("Failed to post surplus insight to Telegram", exc_info=True)
