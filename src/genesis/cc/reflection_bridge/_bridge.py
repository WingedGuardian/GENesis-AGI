"""CCReflectionBridge — core orchestration class.

Dispatches Light/Deep/Strategic reflections to Claude Code background
sessions. Delegates prompt building to _prompts and output handling to
_output submodules.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.autonomy.autonomous_dispatch import AutonomousDispatchRequest
from genesis.autonomy.dispatch_gate import check_dispatch_preconditions
from genesis.autonomy.types import ActionClass, ApprovalDecision, AutonomyCategory
from genesis.awareness.types import Depth
from genesis.cc.contingency import RATE_LIMIT_DEFERRAL_TTL_S
from genesis.cc.reflection_bridge._output import (
    route_deep_output,
    send_to_topic,
    store_reflection_output,
)
from genesis.cc.reflection_bridge._prompts import (
    build_reflection_prompt,
    load_prompt_file,
    system_prompt_for_depth,
)
from genesis.cc.types import CCInvocation, CCModel, EffortLevel, SessionType, background_session_dir
from genesis.db.crud import cc_sessions as cc_sessions_crud
from genesis.observability.call_site_recorder import record_last_run
from genesis.perception.types import ReflectionResult

if TYPE_CHECKING:
    from genesis.cc.protocol import AgentProvider
    from genesis.perception.context import ContextAssembler
    from genesis.reflection.context_gatherer import ContextGatherer
    from genesis.reflection.output_router import OutputRouter
    from genesis.resilience.cc_budget import CCBudgetTracker
    from genesis.resilience.deferred_work import DeferredWorkQueue

logger = logging.getLogger(__name__)


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

_DOWNGRADE_RETRY_BACKOFF_S = 30

_DEFAULT_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "identity"

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
        self._autonomous_dispatcher = None

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

    def set_autonomous_dispatcher(self, dispatcher: object) -> None:
        """Set the API-first autonomous dispatch router."""
        self._autonomous_dispatcher = dispatcher

    # ── Main reflection entry point ───────────────────────────────────

    def _model_for_depth(self, depth: Depth) -> CCModel:
        return _DEPTH_MODEL.get(depth, CCModel.SONNET)

    def _effort_for_context(self, depth: Depth, tick=None, escalation_source: str | None = None) -> EffortLevel:
        """Effort level per depth."""
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
        """Pre-dispatch autonomy gate."""
        required_level = _DEPTH_AUTONOMY_LEVEL.get(depth, 1)
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
        self, depth: Depth, tick, *, db,
        escalation_source: str | None = None,
        skip_approval: bool = False,
    ) -> ReflectionResult:
        """Run Deep or Strategic reflection via CC background session."""
        # Check CC budget before proceeding
        throttle_result = await self._check_throttle(priority=2, work_type="reflection")
        if throttle_result is not None:
            return throttle_result

        # Pre-dispatch autonomy gate
        gate_result = self._check_dispatch_gate(depth)
        if gate_result is not None:
            return gate_result

        model = self._model_for_depth(depth)
        effort = self._effort_for_context(depth, tick=tick, escalation_source=escalation_source)

        # Check if there's pending work (Phase 7 enriched path)
        if self._context_gatherer and depth == Depth.DEEP:
            pending = await self._context_gatherer.detect_pending_work(db)
            if not pending.has_any_work:
                logger.info("Deep reflection skipped — no pending work")
                return ReflectionResult(
                    success=True,
                    reason="No pending work for deep reflection",
                )

        # 1. Build prompt
        prompt, gathered_obs_ids = await build_reflection_prompt(
            depth, tick, db=db,
            context_gatherer=self._context_gatherer,
            context_assembler=self._context_assembler,
            prompt_dir=self._prompt_dir,
        )

        # 2. Prepare CLI invocation
        try:
            from genesis.cc.session_config import SessionConfigBuilder

            _mcp = SessionConfigBuilder()
            if depth == Depth.LIGHT:
                mcp_path = _mcp.build_mcp_config("none")
            elif depth in (Depth.DEEP, Depth.STRATEGIC):
                mcp_path = _mcp.build_mcp_config("reflection")
            else:
                mcp_path = None
        except Exception:
            logger.warning("MCP config generation failed, using defaults", exc_info=True)
            mcp_path = None
        invocation = CCInvocation(
            prompt=prompt,
            model=model,
            effort=effort,
            timeout_s=_DEPTH_TIMEOUT_S.get(depth, 300),
            system_prompt=self._system_prompt_for_depth(depth),
            skip_permissions=True,
            mcp_config=mcp_path,
            working_dir=background_session_dir(),
            stream_idle_timeout_ms=(
                _DEPTH_TIMEOUT_S.get(depth, 300) * 1000
                if depth in (Depth.DEEP, Depth.STRATEGIC)
                else None
            ),
        )
        output = None
        used_cli = True
        session_id = f"api:{depth.value.lower()}"
        if self._autonomous_dispatcher is not None:
            reflection_policy_id = f"reflection_{depth.value.lower()}"
            if not skip_approval:
                # Call-site gating pre-check: if a reflection of this depth
                # is already pending approval, skip scheduling a new one.
                # Skipped when skip_approval=True (resume path — already approved).
                try:
                    pending = await (
                        self._autonomous_dispatcher.approval_gate.find_site_pending(
                            subsystem="reflection",
                            policy_id=reflection_policy_id,
                        )
                    )
                except Exception:
                    logger.warning(
                        "find_site_pending failed for %s; proceeding without pre-check",
                        reflection_policy_id, exc_info=True,
                    )
                    pending = None
                if pending is not None:
                    logger.info(
                        "%s reflection skipped — call site blocked on approval %s",
                        depth.value.title(), pending.get("id"),
                    )
                    return ReflectionResult(
                        success=False,
                        reason=f"awaiting approval {pending.get('id')} for {depth.value} reflection",
                    )

            decision = await self._autonomous_dispatcher.route(
                request=AutonomousDispatchRequest(
                    subsystem="reflection",
                    policy_id=reflection_policy_id,
                    action_label=f"{depth.value.lower()} reflection",
                    messages=[
                        {"role": "system", "content": self._system_prompt_for_depth(depth)},
                        {"role": "user", "content": prompt},
                    ],
                    cli_invocation=invocation,
                    api_call_site_id=_DEPTH_CALL_SITE.get(depth),
                    cli_fallback_allowed=True,
                    approval_required_for_cli=not skip_approval,
                    context={"depth": depth.value},
                ),
            )
            if decision.mode == "blocked":
                return ReflectionResult(success=False, reason=decision.reason)
            if decision.mode == "api":
                output = decision.output
                used_cli = False

        if output is None:
            skill_tags = [f"{depth.value.lower()}-reflection"]
            try:
                sess = await self._session_manager.create_background(
                    session_type=SessionType.BACKGROUND_REFLECTION,
                    model=model,
                    effort=effort,
                    source_tag=f"reflection_{depth.value.lower()}",
                    skill_tags=skill_tags,
                    dispatch_mode="cli",
                )
                session_id = sess["id"]
            except Exception:
                logger.exception("Failed to create background session for %s", depth.value)
                return ReflectionResult(success=False, reason="Session creation failed")

            async def _on_spawn(pid: int) -> None:
                try:
                    await cc_sessions_crud.set_pid(self._db, session_id, pid)
                except Exception:
                    logger.warning("set_pid failed for session %s", session_id[:8], exc_info=True)

            invocation = dataclasses.replace(invocation, on_spawn=_on_spawn)
            output = await self._invoker.run(invocation)

        # Model downgrade response (Layer 2)
        if used_cli and output.downgraded and depth == Depth.STRATEGIC:
            logger.warning(
                "Strategic reflection got downgraded model (%s -> %s), "
                "retrying after %ds backoff",
                model, output.model_used, _DOWNGRADE_RETRY_BACKOFF_S,
            )
            await asyncio.sleep(_DOWNGRADE_RETRY_BACKOFF_S)
            try:
                retry_output = await self._invoker.run(invocation)
            except Exception:
                logger.warning(
                    "Strategic reflection retry failed, using original "
                    "downgraded output",
                    exc_info=True,
                )
                retry_output = None
            if retry_output is not None and not retry_output.downgraded:
                output = retry_output
            elif retry_output is not None:
                logger.warning(
                    "Strategic reflection retry still downgraded (%s), "
                    "proceeding with degraded output",
                    retry_output.model_used,
                )
                output = retry_output
                if self._event_bus:
                    from genesis.observability.types import Severity, Subsystem
                    await self._event_bus.emit(
                        Subsystem.REFLECTION, Severity.WARNING,
                        "reflection.model_degraded",
                        f"Strategic reflection fell back from {model} to "
                        f"{retry_output.model_used} after retry",
                        requested_model=str(model),
                        actual_model=retry_output.model_used or "unknown",
                        depth=depth.value,
                    )
        elif used_cli and output.downgraded and depth == Depth.DEEP:
            logger.warning(
                "Deep reflection model downgraded (%s -> %s), proceeding with weaker model",
                model, output.model_used,
            )
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.REFLECTION, Severity.WARNING,
                    "reflection.model_degraded",
                    f"Deep reflection fell back from {model} to {output.model_used}",
                    requested_model=str(model),
                    actual_model=output.model_used or "unknown",
                    depth=depth.value,
                )

        # Write cc_session_id after all retries resolve (Issue A: must be
        # after the retry block because retries produce new CC sessions).
        if used_cli and session_id and not session_id.startswith("api:") and output.session_id:
            try:
                await cc_sessions_crud.update_cc_session_id(
                    self._db, session_id, cc_session_id=output.session_id,
                )
            except Exception:
                logger.warning(
                    "Failed to write cc_session_id for %s",
                    session_id[:8], exc_info=True,
                )

        if output.is_error:
            logger.error(
                "CC %s reflection failed: %s", depth.value, output.error_message,
            )
            if used_cli:
                await self._session_manager.fail(session_id, reason=output.error_message)
            return ReflectionResult(success=False, reason=output.error_message)

        if used_cli:
            await self._session_manager.complete(
                session_id,
                cost_usd=output.cost_usd,
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
            )

            await record_last_run(
                db, _DEPTH_CALL_SITE.get(depth, f"cc_reflection_{depth.value.lower()}"),
                provider="cc", model_id=output.model_used or str(model),
                response_text=output.text,
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
            )

        # 5. Route output
        routing_failed = False
        if self._output_router and depth == Depth.DEEP:
            try:
                routing_summary = await route_deep_output(
                    output.text, db=db, output_router=self._output_router,
                    gathered_obs_ids=gathered_obs_ids,
                )
                if routing_summary.get("parse_failed") or routing_summary.get("empty_output"):
                    routing_failed = True
            except Exception:
                logger.error(
                    "Deep output routing failed — falling back to legacy store",
                    exc_info=True,
                )
                await store_reflection_output(depth, tick, output, db=db)
        else:
            await store_reflection_output(depth, tick, output, db=db)
            # Mark influenced for non-deep paths (strategic, light) that
            # go through store_reflection_output instead of OutputRouter
            if gathered_obs_ids and output.text and output.text.strip():
                try:
                    from genesis.db.crud import observations
                    await observations.mark_influenced_batch(db, list(gathered_obs_ids))
                except Exception:
                    logger.warning("Failed to mark influenced observations", exc_info=True)

        # 6. Send to topic
        await send_to_topic(session_id, depth, output, topic_manager=self._topic_manager)

        logger.info(
            "%s %s reflection completed (cost=$%.4f, tokens=%d+%d)",
            "CLI" if used_cli else "API",
            depth.value,
            output.cost_usd, output.input_tokens, output.output_tokens,
        )

        if routing_failed:
            return ReflectionResult(
                success=False,
                reason=f"{depth.value} reflection output was unparseable or empty — recorded as failure",
            )

        return ReflectionResult(
            success=True,
            reason=f"{'CLI' if used_cli else 'API'} {depth.value} reflection completed",
        )

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
            system_prompt=load_prompt_file("SELF_ASSESSMENT.md", self._prompt_dir),
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

        if self._output_router:
            from genesis.reflection.output_router import parse_weekly_assessment_output
            parsed = parse_weekly_assessment_output(output.text)
            if parsed.parse_failed:
                logger.error("Weekly assessment output could not be parsed")
                return ReflectionResult(
                    success=False, reason="Weekly assessment output unparseable",
                )
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
            system_prompt=load_prompt_file("QUALITY_CALIBRATION.md", self._prompt_dir),
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
            if parsed.parse_failed:
                logger.error("Quality calibration output could not be parsed")
                return ReflectionResult(
                    success=False, reason="Quality calibration output unparseable",
                )
            await self._output_router.route_calibration(parsed, db)

        logger.info("Quality calibration completed")
        return ReflectionResult(success=True, reason="Quality calibration completed")

    # ── Prompt delegation ─────────────────────────────────────────────

    def _system_prompt_for_depth(self, depth: Depth) -> str:
        return system_prompt_for_depth(depth, self._prompt_dir)

    def _load_prompt_file(self, filename: str) -> str:
        return load_prompt_file(filename, self._prompt_dir)
