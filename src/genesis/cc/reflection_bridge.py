"""CCReflectionBridge — dispatches Light/Deep/Strategic reflections to CC background.

Phase 7 overhaul: uses ContextGatherer for rich context assembly and
OutputRouter for structured result routing. Falls back to simple behavior
when Phase 7 components are not injected.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.autonomy.dispatch_gate import check_dispatch_preconditions
from genesis.autonomy.types import ActionClass, ApprovalDecision, AutonomyCategory
from genesis.awareness.types import Depth, SignalReading, TickResult
from genesis.cc.contingency import RATE_LIMIT_DEFERRAL_TTL_S
from genesis.cc.types import CCInvocation, CCModel, EffortLevel, SessionType, background_session_dir
from genesis.observability.call_site_recorder import record_last_run
from genesis.perception.confidence import load_config as load_confidence_config
from genesis.perception.confidence import should_gate
from genesis.perception.types import LIGHT_FOCUS_ROTATION, MIN_DELTA_CONFIDENCE, ReflectionResult

if TYPE_CHECKING:
    from genesis.cc.protocol import AgentProvider
    from genesis.perception.context import ContextAssembler
    from genesis.reflection.context_gatherer import ContextGatherer
    from genesis.reflection.output_router import OutputRouter
    from genesis.resilience.cc_budget import CCBudgetTracker
    from genesis.resilience.deferred_work import DeferredWorkQueue

logger = logging.getLogger(__name__)


def _format_signal(s: SignalReading) -> str:
    """Format a signal with threshold annotation when available."""
    base = f"{s.name}={s.value}"
    if s.normal_max is not None:
        status = (
            "CRITICAL" if s.critical_threshold is not None and s.value >= s.critical_threshold
            else "WARNING" if s.warning_threshold is not None and s.value >= s.warning_threshold
            else "normal"
        )
        base += f" [{status}]"
    return base


# ── Light reflection focus rotation ──────────────────────────────────

_LIGHT_FOCUS_INSTRUCTIONS: dict[str, str] = {
    "situation": (
        "## Focus: Situation Assessment\n"
        "Assess current system state. Every claim MUST cite a specific signal value.\n"
        "Do NOT produce user_model_updates (set to empty list).\n"
        "Do NOT produce surplus_candidates (set to empty list).\n"
        "Focus on: assessment, patterns, recommendations, escalation."
    ),
    "user_impact": (
        "## Focus: User Impact Analysis\n"
        "Analyze how current conditions affect the user's goals and work.\n"
        "This is the ONLY rotation that produces user_model_updates.\n"
        "Each delta MUST have confidence >= 0.9 and cite specific evidence.\n"
        "Do NOT produce surplus_candidates (set to empty list).\n"
        "Focus on: assessment, user_model_updates, recommendations."
    ),
    "anomaly": (
        "## Focus: Pattern Detection & Anomaly Investigation\n"
        "Look for unusual patterns, unexpected correlations, emerging trends.\n"
        "Flag investigation-worthy items as surplus_candidates.\n"
        "Do NOT produce user_model_updates (set to empty list).\n"
        "Focus on: assessment, patterns, surplus_candidates, escalation."
    ),
}


def _light_focus_area(tick: TickResult) -> str:
    """Derive focus area from tick_id, matching perception engine rotation."""
    import uuid as _uuid

    try:
        tick_number = _uuid.UUID(tick.tick_id).int % 10000
    except ValueError:
        tick_number = int.from_bytes(tick.tick_id.encode()[:8], "big") % 10000
    return LIGHT_FOCUS_ROTATION[tick_number % len(LIGHT_FOCUS_ROTATION)]


_DEPTH_MODEL = {
    Depth.LIGHT: CCModel.HAIKU,
    Depth.DEEP: CCModel.SONNET,
    Depth.STRATEGIC: CCModel.OPUS,
}

_DEPTH_TIMEOUT_S = {
    Depth.LIGHT: 600,
    Depth.DEEP: 1800,
    Depth.STRATEGIC: 3600,
}

_DEPTH_CALL_SITE = {
    Depth.DEEP: "5_deep_reflection",
    Depth.STRATEGIC: "6_strategic_reflection",
    Depth.LIGHT: "4_light_reflection",
}

_DEFAULT_PROMPT_DIR = Path(__file__).resolve().parent.parent / "identity"

_DEPTH_AUTONOMY_LEVEL = {
    Depth.LIGHT: 1,
    Depth.DEEP: 2,
    Depth.STRATEGIC: 3,
}


class CCReflectionBridge:
    """Dispatches Light/Deep/Strategic reflections to Claude Code background sessions."""

    def __init__(
        self,
        *,
        session_manager,
        invoker: AgentProvider,
        db,
        event_bus=None,
        prompt_dir: Path | None = None,
        context_gatherer: ContextGatherer | None = None,
        output_router: OutputRouter | None = None,
        cc_budget: CCBudgetTracker | None = None,
        deferred_queue: DeferredWorkQueue | None = None,
    ):
        self._session_manager = session_manager
        self._invoker = invoker
        self._db = db
        self._event_bus = event_bus
        self._prompt_dir = prompt_dir or _DEFAULT_PROMPT_DIR
        self._context_gatherer = context_gatherer
        self._output_router = output_router
        self._cc_budget = cc_budget
        self._deferred_queue = deferred_queue
        self._context_assembler: ContextAssembler | None = None
        self._topic_manager = None

    # ── Injection setters (for late binding) ──────────────────────────

    def set_context_gatherer(self, gatherer: ContextGatherer) -> None:
        self._context_gatherer = gatherer

    def set_output_router(self, router: OutputRouter) -> None:
        self._output_router = router

    def set_cc_budget(self, budget: CCBudgetTracker) -> None:
        self._cc_budget = budget

    def set_deferred_queue(self, queue: DeferredWorkQueue) -> None:
        self._deferred_queue = queue

    def set_context_assembler(self, assembler: ContextAssembler) -> None:
        """Set ContextAssembler for enriched light reflection prompts."""
        self._context_assembler = assembler

    def set_topic_manager(self, manager: object) -> None:
        """Set TopicManager for routing output to forum topics."""
        self._topic_manager = manager

    # ── Main reflection entry point ───────────────────────────────────

    def _model_for_depth(self, depth: Depth) -> CCModel:
        return _DEPTH_MODEL.get(depth, CCModel.SONNET)

    def _effort_for_context(self, depth: Depth, tick=None, escalation_source: str | None = None) -> EffortLevel:
        """Effort level per depth.

        Deep: fixed Sonnet HIGH (analysis role — adaptive effort moves to the
        executor in V4). Strategic: Opus MAX. Light: LOW (CC Haiku fallback).
        escalation_source is logged but no longer affects effort for deep.
        """
        if depth == Depth.STRATEGIC:
            return EffortLevel.MAX
        if depth == Depth.LIGHT:
            return EffortLevel.LOW
        # Deep: fixed HIGH. Adaptive effort is a V4 executor concern.
        # GROUNDWORK(v4-executor): escalation_source will drive executor effort.
        if escalation_source:
            logger.debug("Deep reflection triggered by %s (effort fixed at HIGH)", escalation_source)
        return EffortLevel.HIGH

    async def _check_throttle(self, priority: int, work_type: str) -> ReflectionResult | None:
        """Check CC budget throttling. Returns a deferred result if throttled, None otherwise."""
        if self._cc_budget is None:
            return None
        if not await self._cc_budget.should_throttle(requested_priority=priority):
            return None
        logger.warning("CC throttled, deferring %s", work_type)
        if self._deferred_queue:
            import json
            await self._deferred_queue.enqueue(
                work_type=work_type,
                call_site_id=None,
                priority=30 if work_type == "reflection" else 40,
                payload=json.dumps({"work_type": work_type}),
                reason="CC throttled",
                staleness_policy="ttl",
                staleness_ttl_s=RATE_LIMIT_DEFERRAL_TTL_S,
            )
        return ReflectionResult(success=False, reason=f"CC throttled — {work_type} deferred")

    def _check_dispatch_gate(
        self, depth: Depth, *, earned_level: int | None = None,
    ) -> ReflectionResult | None:
        """Pre-dispatch autonomy gate. Returns a rejection result if blocked, None to proceed.

        In V3, only hard ceiling violations block. Earned-level checks are
        logged as warnings but allowed — trust progression is a V4 concern.
        """
        required_level = _DEPTH_AUTONOMY_LEVEL.get(depth, 1)
        # Use ceiling as earned_level so only hard ceiling violations block.
        # When autonomy manager provides real earned levels (pre-V4), pass
        # them explicitly to enable trust-based gating.
        if earned_level is None:
            earned_level = 3  # BACKGROUND_COGNITIVE ceiling
        decision = check_dispatch_preconditions(
            category=AutonomyCategory.BACKGROUND_COGNITIVE.value,
            required_level=required_level,
            action_class=ActionClass.REVERSIBLE,
            earned_level=earned_level,
        )
        if decision == ApprovalDecision.BLOCK:
            logger.warning(
                "Dispatch gate BLOCKED %s reflection (required L%d, ceiling exceeded)",
                depth.value, required_level,
            )
            return ReflectionResult(
                success=False,
                reason=f"Dispatch gate blocked {depth.value} reflection (L{required_level} exceeds ceiling)",
            )
        if decision == ApprovalDecision.PROPOSE:
            logger.info(
                "Dispatch gate PROPOSE for %s reflection (required L%d) — proceeding in V3",
                depth.value, required_level,
            )
        return None

    async def reflect(
        self, depth: Depth, tick: TickResult, *, db,
        escalation_source: str | None = None,
    ) -> ReflectionResult:
        """Run Deep or Strategic reflection via CC background session."""
        logger.info(
            "Reflection dispatch starting: depth=%s, tick=%s, escalation=%s",
            depth.value, tick.tick_id[:8], escalation_source,
        )

        # Check CC budget before proceeding
        throttle_result = await self._check_throttle(priority=2, work_type="reflection")
        if throttle_result is not None:
            logger.info("Reflection %s THROTTLED: %s", depth.value, throttle_result.reason)
            return throttle_result

        # Pre-dispatch autonomy gate
        gate_result = self._check_dispatch_gate(depth)
        if gate_result is not None:
            logger.info("Reflection %s GATE-BLOCKED: %s", depth.value, gate_result.reason)
            return gate_result

        model = self._model_for_depth(depth)
        effort = self._effort_for_context(depth, tick=tick, escalation_source=escalation_source)

        # Check if there's pending work (Phase 7 enriched path)
        if self._context_gatherer and depth == Depth.DEEP:
            pending = await self._context_gatherer.detect_pending_work(db)
            logger.info(
                "Deep reflection pending work: has_any=%s, jobs=%s "
                "(obs=%d, surplus=%d, skills=%d, cog_stale=%s)",
                pending.has_any_work,
                [j.value for j in pending.active_jobs],
                pending.observation_backlog,
                pending.surplus_pending,
                pending.skills_needing_review,
                pending.cognitive_regeneration,
            )
            if not pending.has_any_work:
                logger.info("Deep reflection skipped — no pending work")
                return ReflectionResult(
                    success=True,
                    reason="No pending work for deep reflection",
                )

        # 1. Create background session with skill tags
        skill_tags = [f"{depth.value.lower()}-reflection"]
        try:
            sess = await self._session_manager.create_background(
                session_type=SessionType.BACKGROUND_REFLECTION,
                model=model,
                effort=effort,
                source_tag=f"reflection_{depth.value.lower()}",
                skill_tags=skill_tags,
            )
        except Exception:
            logger.exception("Failed to create background session for %s", depth.value)
            return ReflectionResult(success=False, reason="Session creation failed")

        # 2. Build prompt (enriched if context_gatherer available)
        prompt = await self._build_reflection_prompt(depth, tick, db=db)

        # 3. Invoke CC
        # Light reflections don't use tools — skip MCP server startup to avoid
        # failures from concurrent MCP processes in the bridge context.
        # NOTE: --bare is NOT used — it skips auth credential loading in recent
        # CC versions, causing "Not logged in" failures. no_mcp.json achieves
        # the same MCP-skip effect without breaking auth.
        no_mcp = str(Path(__file__).resolve().parents[3] / "config" / "no_mcp.json")
        invocation = CCInvocation(
            prompt=prompt,
            model=model,
            effort=effort,
            timeout_s=_DEPTH_TIMEOUT_S.get(depth, 300),
            system_prompt=self._system_prompt_for_depth(depth),
            skip_permissions=True,
            mcp_config=no_mcp if depth == Depth.LIGHT else None,
            working_dir=background_session_dir(),
            stream_idle_timeout_ms=(
                _DEPTH_TIMEOUT_S.get(depth, 300) * 1000
                if depth in (Depth.DEEP, Depth.STRATEGIC)
                else None
            ),
        )
        output = await self._invoker.run(invocation)

        if output.is_error:
            logger.error(
                "CC %s reflection failed: %s", depth.value, output.error_message,
            )
            await self._session_manager.fail(sess["id"], reason=output.error_message)
            return ReflectionResult(success=False, reason=output.error_message)

        # 4. Complete session with cost data
        await self._session_manager.complete(
            sess["id"],
            cost_usd=output.cost_usd,
            input_tokens=output.input_tokens,
            output_tokens=output.output_tokens,
        )

        # 4b. Record last run for neural monitor
        await record_last_run(
            db, _DEPTH_CALL_SITE.get(depth, f"cc_reflection_{depth.value.lower()}"),
            provider="cc", model_id=output.model_used or str(model),
            response_text=output.text,
            input_tokens=output.input_tokens,
            output_tokens=output.output_tokens,
        )

        # 5. Route output (Phase 7 enriched path or legacy)
        if self._output_router and depth == Depth.DEEP:
            try:
                await self._route_deep_output(output.text, db=db)
            except Exception:
                logger.error(
                    "Deep output routing failed — falling back to legacy store",
                    exc_info=True,
                )
                await self._store_reflection_output(depth, tick, output, db=db)
        else:
            await self._store_reflection_output(depth, tick, output, db=db)

        # 6. Send summary to forum topic (if TopicManager available)
        await self._send_to_topic(sess["id"], depth, output)

        logger.info(
            "CC %s reflection completed (cost=$%.4f, tokens=%d+%d)",
            depth.value, output.cost_usd, output.input_tokens, output.output_tokens,
        )
        return ReflectionResult(success=True, reason=f"CC {depth.value} reflection completed")

    # ── Weekly jobs ───────────────────────────────────────────────────

    async def run_weekly_assessment(self, db) -> ReflectionResult:
        """Run weekly self-assessment via CC background session."""
        throttle_result = await self._check_throttle(priority=3, work_type="weekly_assessment")
        if throttle_result is not None:
            return throttle_result

        gate_result = self._check_dispatch_gate(Depth.DEEP)
        if gate_result is not None:
            return gate_result

        if not self._context_gatherer:
            return ReflectionResult(success=False, reason="No context gatherer available")

        data = await self._context_gatherer.gather_for_assessment(db)

        try:
            sess = await self._session_manager.create_background(
                session_type=SessionType.BACKGROUND_REFLECTION,
                model=CCModel.SONNET,
                effort=EffortLevel.HIGH,
                source_tag="weekly_assessment",
                skill_tags=["self-assessment"],
            )
        except Exception:
            logger.exception("Failed to create assessment session")
            return ReflectionResult(success=False, reason="Session creation failed")

        prompt = (
            "Perform a weekly self-assessment.\n\n"
            f"## Assessment Data\n\n```json\n{json.dumps(data, indent=2)}\n```\n\n"
            "Evaluate each of the 6 dimensions using this data."
        )

        invocation = CCInvocation(
            prompt=prompt,
            model=CCModel.SONNET,
            effort=EffortLevel.HIGH,
            system_prompt=self._load_prompt_file("SELF_ASSESSMENT.md"),
            skip_permissions=True,
            working_dir=background_session_dir(),
        )
        output = await self._invoker.run(invocation)

        if output.is_error:
            await self._session_manager.fail(sess["id"], reason=output.error_message)
            return ReflectionResult(success=False, reason=output.error_message)

        await self._session_manager.complete(
            sess["id"],
            cost_usd=output.cost_usd,
            input_tokens=output.input_tokens,
            output_tokens=output.output_tokens,
        )

        await record_last_run(
            db, "14_weekly_self_assessment",
            provider="cc", model_id=output.model_used or str(CCModel.SONNET),
            response_text=output.text,
            input_tokens=output.input_tokens,
            output_tokens=output.output_tokens,
        )

        # Route output
        if self._output_router:
            from genesis.reflection.output_router import parse_weekly_assessment_output
            parsed = parse_weekly_assessment_output(output.text)
            await self._output_router.route_assessment(parsed, db)

        logger.info("Weekly self-assessment completed")
        return ReflectionResult(success=True, reason="Weekly assessment completed")

    async def run_quality_calibration(self, db) -> ReflectionResult:
        """Run weekly quality calibration via CC background session."""
        throttle_result = await self._check_throttle(priority=3, work_type="quality_calibration")
        if throttle_result is not None:
            return throttle_result

        gate_result = self._check_dispatch_gate(Depth.DEEP)
        if gate_result is not None:
            return gate_result

        if not self._context_gatherer:
            return ReflectionResult(success=False, reason="No context gatherer available")

        data = await self._context_gatherer.gather_for_calibration(db)

        try:
            sess = await self._session_manager.create_background(
                session_type=SessionType.BACKGROUND_REFLECTION,
                model=CCModel.SONNET,
                effort=EffortLevel.HIGH,
                source_tag="quality_calibration",
                skill_tags=["strategic-reflection"],
            )
        except Exception:
            logger.exception("Failed to create calibration session")
            return ReflectionResult(success=False, reason="Session creation failed")

        prompt = (
            "Perform a quality calibration check.\n\n"
            f"## Calibration Data\n\n```json\n{json.dumps(data, indent=2)}\n```\n\n"
            "Evaluate quality drift and identify quarantine candidates."
        )

        invocation = CCInvocation(
            prompt=prompt,
            model=CCModel.SONNET,
            effort=EffortLevel.HIGH,
            system_prompt=self._load_prompt_file("QUALITY_CALIBRATION.md"),
            skip_permissions=True,
            working_dir=background_session_dir(),
        )
        output = await self._invoker.run(invocation)

        if output.is_error:
            await self._session_manager.fail(sess["id"], reason=output.error_message)
            return ReflectionResult(success=False, reason=output.error_message)

        await self._session_manager.complete(
            sess["id"],
            cost_usd=output.cost_usd,
            input_tokens=output.input_tokens,
            output_tokens=output.output_tokens,
        )

        await record_last_run(
            db, "16_quality_calibration",
            provider="cc", model_id=output.model_used or str(CCModel.SONNET),
            response_text=output.text,
            input_tokens=output.input_tokens,
            output_tokens=output.output_tokens,
        )

        if self._output_router:
            from genesis.reflection.output_router import parse_quality_calibration_output
            parsed = parse_quality_calibration_output(output.text)
            await self._output_router.route_calibration(parsed, db)

        logger.info("Quality calibration completed")
        return ReflectionResult(success=True, reason="Quality calibration completed")

    # ── Prompt building ───────────────────────────────────────────────

    async def _build_reflection_prompt(self, depth: Depth, tick: TickResult, *, db) -> str:
        """Build prompt — enriched if context_gatherer available, simple otherwise."""

        # Phase 7 enriched path
        if self._context_gatherer and depth == Depth.DEEP:
            return await self._build_enriched_prompt(tick, db=db)

        # Legacy simple path
        from genesis.db.crud import cognitive_state

        signals_summary = ", ".join(
            _format_signal(s) for s in tick.signals[:10]
        ) if tick.signals else "none"

        scores_summary = ", ".join(
            f"{s.depth.value}={s.final_score:.2f}" for s in tick.scores
        ) if tick.scores else "none"

        cog_state = await cognitive_state.render(db)

        # Light: focus-aware prompt — enriched if context_assembler available
        if depth == Depth.LIGHT:
            focus = _light_focus_area(tick)
            focus_instruction = _LIGHT_FOCUS_INSTRUCTIONS.get(focus, _LIGHT_FOCUS_INSTRUCTIONS["situation"])

            if self._context_assembler:
                return await self._build_light_prompt_enriched(
                    tick, focus, focus_instruction, db=db,
                )

            # Fallback: thin prompt (when context_assembler not injected)
            return (
                f"Perform a Light reflection.\n\n"
                f"Tick ID: {tick.tick_id}\n"
                f"Timestamp: {tick.timestamp}\n"
                f"Trigger: {tick.trigger_reason or 'scheduled'}\n"
                f"Signals: {signals_summary}\n"
                f"Depth scores: {scores_summary}\n\n"
                f"## Current Cognitive State\n\n{cog_state}\n\n"
                f"{focus_instruction}\n\n"
                f'Set "focus_area": "{focus}" in your JSON output.'
            )

        return (
            f"Perform a {depth.value} reflection.\n\n"
            f"Tick ID: {tick.tick_id}\n"
            f"Timestamp: {tick.timestamp}\n"
            f"Trigger: {tick.trigger_reason or 'scheduled'}\n"
            f"Signals: {signals_summary}\n"
            f"Depth scores: {scores_summary}\n\n"
            f"## Current Cognitive State\n\n{cog_state}\n\n"
            f"Analyze the current state, identify patterns and observations, "
            f"and provide actionable insights."
        )

    async def _build_enriched_prompt(self, tick: TickResult, *, db) -> str:
        """Build rich prompt using ContextGatherer data."""
        bundle = await self._context_gatherer.gather(db)

        parts = [
            "Perform a Deep reflection.\n",
            f"Tick ID: {tick.tick_id}",
            f"Timestamp: {tick.timestamp}",
            f"Trigger: {tick.trigger_reason or 'scheduled'}",
        ]

        # Signals
        if tick.signals:
            signals = ", ".join(_format_signal(s) for s in tick.signals[:10])
            parts.append(f"\n## Signals\n{signals}")

        # Cognitive state
        parts.append(f"\n## Current Cognitive State\n{bundle.cognitive_state}")

        # Pending work summary
        jobs = bundle.pending_work.active_jobs
        if jobs:
            parts.append("\n## Active Jobs\n" + ", ".join(j.value for j in jobs))

        # Recent observations (for consolidation)
        if bundle.recent_observations and bundle.pending_work.memory_consolidation:
            obs_summary = json.dumps([
                {"id": o.get("id", ""), "type": o.get("type", ""),
                 "content": o.get("content", "")[:200], "created_at": o.get("created_at", "")}
                for o in bundle.recent_observations[:30]
            ], indent=2)
            parts.append(f"\n## Recent Observations (for consolidation)\n```json\n{obs_summary}\n```")

        # Intelligence digest (replaces old surplus staging items section)
        if bundle.intelligence_digest:
            parts.append(f"\n## Intelligence Digest (since last Deep cycle)\n{bundle.intelligence_digest}")

        # Procedure stats
        stats = bundle.procedure_stats
        if stats.total_active > 0:
            parts.append(
                f"\n## Procedure Stats\n"
                f"Active: {stats.total_active}, Quarantined: {stats.total_quarantined}, "
                f"Avg success rate: {stats.avg_success_rate:.1%}"
            )
            if stats.low_performers:
                parts.append(f"Low performers: {json.dumps(stats.low_performers)}")

        # Cost summary
        cost = bundle.cost_summary
        parts.append(
            f"\n## Cost Summary\n"
            f"Today: ${cost.daily_usd:.4f} ({cost.daily_budget_pct:.0%} of daily budget)\n"
            f"This week: ${cost.weekly_usd:.4f} ({cost.weekly_budget_pct:.0%} of weekly)\n"
            f"This month: ${cost.monthly_usd:.4f} ({cost.monthly_budget_pct:.0%} of monthly)"
        )

        # Recent user conversation
        if bundle.recent_conversations:
            conv_lines = []
            for turn in bundle.recent_conversations:
                ts = turn.get("timestamp", "")
                text = turn.get("text", "")
                conv_lines.append(f"[{ts}] {text}")
            parts.append(
                "\n## Recent User Conversation\n"
                "The user has been discussing the following in the current CLI session:\n"
                + "\n".join(conv_lines)
            )

        return "\n".join(parts)

    async def _build_light_prompt_enriched(
        self,
        tick: TickResult,
        focus: str,
        focus_instruction: str,
        *,
        db,
    ) -> str:
        """Build light reflection prompt using ContextAssembler for parity with perception engine.

        Focus-aware context injection: each focus area gets only the context it needs.
        - situation: signals + cognitive state (lean)
        - user_impact: + user profile + user model
        - anomaly: + recent observations (memory hits)
        """
        ctx = await self._context_assembler.assemble(Depth.LIGHT, tick, db=db)

        parts = [
            "Perform a Light reflection.\n",
            f"Tick ID: {tick.tick_id}",
            f"Timestamp: {tick.timestamp}",
            f"Trigger: {tick.trigger_reason or 'scheduled'}\n",
            f"## Signals\n{ctx.signals_text}",
            f"\n## Current Cognitive State\n{ctx.cognitive_state or '(none)'}",
        ]

        # Focus-specific context injection
        if focus == "user_impact":
            if ctx.user_profile:
                parts.append(f"\n## User Profile\n{ctx.user_profile}")
            if ctx.user_model:
                parts.append(f"\n## User Model\n{ctx.user_model}")
        elif focus == "anomaly":
            if ctx.memory_hits:
                parts.append(f"\n## Recent Observations\n{ctx.memory_hits}")

        parts.append(f"\n{focus_instruction}")
        parts.append(f'\nSet "focus_area": "{focus}" in your JSON output.')
        return "\n".join(parts)

    # ── Prompt file loading ───────────────────────────────────────────

    _PROMPT_FILES = {
        Depth.LIGHT: "REFLECTION_LIGHT.md",
        Depth.DEEP: "REFLECTION_DEEP.md",
        Depth.STRATEGIC: "REFLECTION_STRATEGIC.md",
    }

    _FALLBACK_PROMPTS = {
        Depth.LIGHT: (
            "You are Genesis performing a Light reflection — a quick sanity check. "
            "Note anomalies, check if escalation is needed. "
            "Output JSON with 'observations', 'escalate_to_deep', 'summary'."
        ),
        Depth.DEEP: (
            "You are Genesis performing a Deep reflection. "
            "Analyze recent signals and observations for meaningful patterns. "
            "Output structured JSON with 'observations', 'patterns', 'recommendations'."
        ),
        Depth.STRATEGIC: (
            "You are Genesis performing a Strategic reflection. "
            "Think broadly about long-term patterns, goals, and system evolution. "
            "Output structured JSON with 'observations', 'patterns', 'recommendations'."
        ),
    }

    def _system_prompt_for_depth(self, depth: Depth) -> str:
        filename = self._PROMPT_FILES.get(depth)
        if filename:
            # Check for model-specific variant first (e.g. REFLECTION_LIGHT_HAIKU.md)
            model = _DEPTH_MODEL.get(depth)
            if model:
                stem = filename.rsplit(".", 1)[0]
                model_file = f"{stem}_{model.value.upper()}.md"
                model_path = self._prompt_dir / model_file
                if model_path.exists():
                    return model_path.read_text()
            # Fallback to generic
            path = self._prompt_dir / filename
            if path.exists():
                return path.read_text()
        return self._FALLBACK_PROMPTS.get(depth, self._FALLBACK_PROMPTS[Depth.DEEP])

    def _load_prompt_file(self, filename: str) -> str:
        """Load a CAPS markdown prompt file with fallback."""
        path = self._prompt_dir / filename
        if path.exists():
            return path.read_text()
        logger.warning("Prompt file %s not found, using minimal fallback", filename)
        return "Perform the task described in the user message. Output valid JSON."

    # ── Output handling ───────────────────────────────────────────────

    async def _route_deep_output(self, raw_text: str, *, db) -> None:
        """Parse and route deep reflection output via OutputRouter."""
        from genesis.reflection.output_router import parse_deep_reflection_output
        parsed = parse_deep_reflection_output(raw_text)
        await self._output_router.route(parsed, db)

    async def _store_reflection_output(self, depth, tick, output, *, db) -> None:
        """Legacy: store CC reflection output as a single observation."""
        import re

        from genesis.db.crud import observations

        now = datetime.now(UTC).isoformat()
        source = f"cc_reflection_{depth.value.lower()}"

        # Primary reflection output (with dedup)
        content = json.dumps({
            "tick_id": tick.tick_id,
            "depth": depth.value,
            "cc_output": output.text[:2000],
            "model_used": output.model_used,
            "cost_usd": output.cost_usd,
            "input_tokens": output.input_tokens,
            "output_tokens": output.output_tokens,
        }, sort_keys=True)
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        if not await observations.exists_by_hash(
            db, source=source, content_hash=content_hash, unresolved_only=True,
        ):
            await observations.create(
                db,
                id=str(uuid.uuid4()),
                source=source,
                type="reflection_output",
                content=content,
                priority="high" if depth == Depth.STRATEGIC else "medium",
                created_at=now,
                content_hash=content_hash,
                skip_if_duplicate=True,
            )

        # Light: parse JSON for escalation, user_model_deltas, surplus_candidates
        if depth == Depth.LIGHT:
            try:
                json_match = re.search(r"```json\s*(.*?)\s*```", output.text, re.DOTALL)
                raw_json = json_match.group(1) if json_match else output.text
                data = json.loads(raw_json)

                # Confidence gate — check before extracting downstream data
                cfg = load_confidence_config()
                cc_confidence = float(data.get("confidence", 0.7))
                gated, gate_msg = should_gate(cc_confidence, cfg.observation_write)
                if gate_msg:
                    logger.info("CC bridge light confidence gate: %s", gate_msg)
                # Escalation check — runs BEFORE confidence gate (escalation is a safety mechanism)
                if data.get("escalate_to_deep"):
                    esc_reason = data.get("escalation_reason", "light CC reflection requested escalation")
                    logger.info("Light CC reflection requested deep escalation: %s", esc_reason)
                    esc_hash = hashlib.sha256(esc_reason.encode()).hexdigest()
                    if not await observations.exists_by_hash(
                        db, source="awareness_loop", content_hash=esc_hash, unresolved_only=True,
                    ):
                        await observations.create(
                            db,
                            id=str(uuid.uuid4()),
                            source="awareness_loop",
                            type="light_escalation_pending",
                            content=esc_reason,
                            priority="high",
                            created_at=now,
                            content_hash=esc_hash,
                        )

                # Confidence gate — skip delta/surplus extraction if below threshold
                if gated:
                    return

                # Extract user_model_deltas — same gates as perception engine writer
                for delta in data.get("user_model_updates", []):
                    try:
                        conf = float(delta.get("confidence", 0))
                        if conf < MIN_DELTA_CONFIDENCE:
                            continue
                        field_val = str(delta.get("field", "")), str(delta.get("value", ""))
                        if not all(field_val):
                            continue
                        dedup_key = json.dumps({"field": field_val[0], "value": field_val[1]}, sort_keys=True)
                        delta_hash = hashlib.sha256(dedup_key.encode()).hexdigest()
                        if await observations.exists_by_hash(db, source="reflection", content_hash=delta_hash):
                            continue
                        delta_content = json.dumps({
                            "field": field_val[0], "value": field_val[1],
                            "evidence": str(delta.get("evidence", "")),
                            "confidence": conf,
                        }, sort_keys=True)
                        await observations.create(
                            db,
                            id=str(uuid.uuid4()),
                            source="reflection",
                            type="user_model_delta",
                            content=delta_content,
                            priority="medium",
                            created_at=now,
                            content_hash=delta_hash,
                        )
                    except (TypeError, ValueError, AttributeError):
                        logger.debug("Skipping malformed user_model_delta from CC output")

                # Extract surplus_candidates → surplus_insights table
                from genesis.db.crud import surplus as surplus_crud

                for candidate in data.get("surplus_candidates", []):
                    stripped = str(candidate).strip() if candidate else ""
                    if not stripped:
                        continue
                    try:
                        sc_id = hashlib.sha256(stripped.encode()).hexdigest()[:16]
                        await surplus_crud.upsert(
                            db,
                            id=f"light-{sc_id}",
                            content=stripped,
                            source_task_type="light_reflection_candidate",
                            generating_model="cc_bridge",
                            drive_alignment="curiosity",
                            confidence=float(data.get("confidence", 0.5)),
                            created_at=now,
                            ttl=(datetime.now(UTC) + timedelta(days=7)).isoformat(),
                        )
                    except Exception:
                        logger.error("Failed to upsert surplus candidate from CC output", exc_info=True)

            except (json.JSONDecodeError, AttributeError):
                logger.debug("Could not parse JSON from light CC output")

        # Extract and store focus_next_week from strategic output
        if depth == Depth.STRATEGIC:
            try:
                json_match = re.search(r"```json\s*(.*?)\s*```", output.text, re.DOTALL)
                raw_json = json_match.group(1) if json_match else output.text
                data = json.loads(raw_json)
                focus_week = data.get("focus_next_week", "")
                if focus_week:
                    # NOTE: replace_section is destructive — overwrites all
                    # pending_actions content. Known limitation (post-phase-9 fix).
                    from genesis.db.crud import cognitive_state
                    await cognitive_state.replace_section(
                        db,
                        section="pending_actions",
                        id=str(uuid.uuid4()),
                        content=f"## Strategic Focus (This Week)\n{focus_week}",
                        generated_by="strategic_reflection",
                        created_at=now,
                    )
                    logger.info("Strategic focus_next_week stored: %s", focus_week[:100])
            except (json.JSONDecodeError, AttributeError):
                logger.debug("Could not parse focus_next_week from strategic output")

        # Store a consolidated reflection summary for embedding.
        # Deep reflections get their summary via OutputRouter.route() — skip here.
        # For strategic/light, extract structured content if available.
        if depth != Depth.DEEP:
            summary_parts = []
            try:
                data = json.loads(output.text)
                if isinstance(data, dict):
                    if data.get("assessment"):
                        summary_parts.append(str(data["assessment"])[:1500])
                    if data.get("focus_next") or data.get("focus_next_week"):
                        focus = data.get("focus_next_week") or data.get("focus_next", "")
                        summary_parts.append(f"Focus: {focus}")
                    for obs in (data.get("observations") or [])[:3]:
                        if obs:
                            summary_parts.append(str(obs))
            except (json.JSONDecodeError, TypeError):
                # Fallback: use first 2000 chars of raw output for non-JSON responses
                if output.text.strip():
                    summary_parts.append(output.text[:2000])
            if summary_parts:
                summary_text = "\n\n".join(summary_parts)[:4000]
                summary_hash = hashlib.sha256(summary_text.encode()).hexdigest()
                if not await observations.exists_by_hash(
                    db, source=source, content_hash=summary_hash, unresolved_only=True,
                ):
                    await observations.create(
                        db,
                        id=str(uuid.uuid4()),
                        source=source,
                        type="reflection_summary",
                        content=summary_text,
                        priority="medium",
                        created_at=now,
                        content_hash=summary_hash,
                        skip_if_duplicate=True,
                    )

    async def _send_to_topic(self, session_id: str, depth, output) -> None:
        """Send a reflection summary to the depth-specific topic."""
        if not self._topic_manager:
            return
        category = f"reflection_{depth.value.lower()}"
        summary = (
            f"<b>{depth.value} Reflection</b>\n\n"
            f"{output.text[:3000]}"
        )
        try:
            await self._topic_manager.send_to_category(category, summary)
        except Exception:
            logger.error("Failed to send reflection output to topic", exc_info=True)
