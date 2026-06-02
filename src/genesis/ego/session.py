"""Ego session — ephemeral CC session with tool access.

Orchestrates: context assembly → CC invocation → output parsing →
cycle storage → proposal creation → follow-up recording.

Each ego cycle is a fresh CC session (no --resume). The system prompt
contains ONLY the static identity (EGO_SESSION.md), making it fully
cacheable. Operational context is injected via the user message.
Durable knowledge lives in the memory system (memory_store/recall).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.autonomy.autonomous_dispatch import AutonomousDispatchRequest
from genesis.cc.types import (
    CCInvocation,
    CCModel,
    EffortLevel,
    SessionType,
    background_session_dir,
)
from genesis.db.crud import ego as ego_crud
from genesis.ego.types import (
    NEUTRAL_STATUS,
    EgoConfig,
    EgoCycle,
)
from genesis.observability.session_context import set_session_id as _set_obs_session

if TYPE_CHECKING:
    import aiosqlite

    from genesis.cc.direct_session import DirectSessionRunner
    from genesis.cc.protocol import AgentProvider
    from genesis.cc.session_manager import SessionManager
    from genesis.ego.compaction import CompactionEngine
    from genesis.ego.context import EgoContextBuilder
    from genesis.ego.dispatch import EgoDispatcher
    from genesis.ego.focus import FocusResult
    from genesis.ego.proposals import ProposalWorkflow
    from genesis.ego.signals import EgoSignal
    from genesis.observability.events import GenesisEventBus

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "identity" / "EGO_SESSION.md"
_DEFAULT_CALL_SITE = "7_ego_cycle"
_DEFAULT_FOCUS_SUMMARY_KEY = "ego_focus_summary"


class CycleBlockedError(Exception):
    """Raised when the ego cycle is blocked by an approval gate.

    This is a gate, not a failure -- the circuit breaker should NOT
    count it toward consecutive failures.
    """


class EgoSession:
    """Ephemeral CC session for ego thinking cycles.

    One instance is created at runtime startup and reused across cycles.
    Each cycle creates a fresh CC session (no --resume). The system
    prompt is the static identity only (cacheable); operational context
    goes in the user message.

    The class is ego-generic: both the user ego and Genesis ego share
    this infrastructure. The caller configures identity via
    ``prompt_path``, ``call_site``, etc.
    """

    def __init__(
        self,
        *,
        invoker: AgentProvider,
        session_manager: SessionManager,
        compaction_engine: CompactionEngine,
        context_builder: EgoContextBuilder,
        proposal_workflow: ProposalWorkflow,
        dispatcher: EgoDispatcher,
        config: EgoConfig,
        db: aiosqlite.Connection,
        event_bus: GenesisEventBus | None = None,
        direct_session_runner: DirectSessionRunner | None = None,
        mcp_config_path: str | None = None,
        prompt_path: Path | None = None,
        call_site: str | None = None,
        # Legacy param accepted for backward compat; no longer used.
        session_id_key: str | None = None,
        focus_summary_key: str | None = None,
        source_tag: str | None = None,
        # Unified cognitive loop — optional router for focus selector (PR 1).
        # Wired in PR 2 via init/ego.py. When None, focus selection
        # falls back to highest-priority signal (no LLM call).
        router: object | None = None,
    ) -> None:
        self._invoker = invoker
        self._session_manager = session_manager
        self._compaction = compaction_engine
        self._context_builder = context_builder
        self._proposals = proposal_workflow
        self._dispatcher = dispatcher
        self._config = config
        self._db = db
        self._event_bus = event_bus
        self._direct_session_runner = direct_session_runner
        self._autonomous_dispatcher = None
        self._proposal_gate = None
        self._outreach_pipeline = None
        self._mcp_config_path = mcp_config_path
        self._call_site = call_site or _DEFAULT_CALL_SITE
        self._focus_summary_key = focus_summary_key or _DEFAULT_FOCUS_SUMMARY_KEY
        self._source_tag = source_tag or "ego_cycle"
        self._router = router  # For unified cognitive loop focus selector
        self._focus_selector = None  # Lazy-init in _perceive
        self._sweep_lock = asyncio.Lock()
        self._last_realist_cost_usd = 0.0  # accumulated by _filter_proposals
        # Cache the static system prompt (read once, not every cycle)
        actual_prompt_path = prompt_path or _DEFAULT_PROMPT_PATH
        if actual_prompt_path.exists():
            self._static_prompt = actual_prompt_path.read_text()
        else:
            logger.warning("Ego prompt not found at %s", actual_prompt_path)
            self._static_prompt = (
                "You are Genesis's executive function. "
                "Output valid JSON matching the ego output schema."
            )
        # Prompt versioning: hash the static prompt for outcome linkage
        from genesis.db.crud.prompt_versions import compute_prompt_hash
        self._prompt_hash = compute_prompt_hash(self._static_prompt)
        self._prompt_version_recorded = False

    def set_autonomous_dispatcher(self, dispatcher: object) -> None:
        self._autonomous_dispatcher = dispatcher

    def set_proposal_gate(self, gate: object) -> None:
        """Inject the ProposalDispatchGate for evaluating proposals at dispatch.

        The gate is opaque to the ego — it silently blocks proposals that
        exceed the current autonomy level for their action domain.
        """
        self._proposal_gate = gate

    def set_outreach_pipeline(self, pipeline: object) -> None:
        """Inject the OutreachPipeline for ego notification delivery.

        Late-binding because the outreach pipeline may not be available at
        EgoSession construction time (same pattern as autonomous_dispatcher).
        """
        self._outreach_pipeline = pipeline

    # -- Public API --------------------------------------------------------

    async def run_unified_cycle(
        self,
        signals: list[EgoSignal],
        *,
        model_override: str | None = None,
        effort_override: str | None = None,
    ) -> EgoCycle | None:
        """Execute one ego cycle via the perceive→think→act→learn pipeline.

        Entry point for all ego cycles via the unified cognitive loop.

        Parameters
        ----------
        signals:
            List of ``EgoSignal`` objects drained from the SignalQueue.
        model_override:
            Override model selection (takes precedence over config).
        effort_override:
            Override effort level (e.g. morning report uses "low").

        Returns the stored EgoCycle, or None if perception found nothing
        actionable or the CC invocation failed.

        Raises:
            CycleBlockedError: Approval gate blocked the cycle.
        """
        if not signals:
            return None

        # 1. PERCEIVE — focus selection
        focus = await self._perceive(signals)
        if focus is None:
            return None

        # 2. THINK — context, prompt, invoke

        # Context assembly with focus-based section weights.
        # Weights come from the focus selector's lookup table (focus.py).
        # Sections marked "skip" are omitted, "light" get 1-2 line summaries,
        # "always" sections (user_model, intentions, directives, output_contract)
        # are never skipped.
        dynamic_context = await self._compaction.assemble_context(
            context_builder=self._context_builder,
            context_weights=focus.context_weights if focus else None,
            focus_id=focus.focus_id if focus else None,
        )

        # Focus-specific prompt
        user_prompt = self._build_focused_prompt(
            dynamic_context=dynamic_context,
            focus=focus,
        )

        # Model + effort from config, with signal-based overrides
        model = CCModel(model_override or self._config.model)
        try:
            effort = EffortLevel(effort_override or self._config.default_effort)
        except ValueError:
            logger.warning("Invalid effort_override %r — using default", effort_override)
            effort = EffortLevel(self._config.default_effort)

        # System prompt is identity ONLY (cacheable)
        # Record prompt version on first use
        if not self._prompt_version_recorded and self._db is not None:
            try:
                from genesis.db.crud.prompt_versions import record_version
                await record_version(
                    self._db,
                    prompt_hash=self._prompt_hash,
                    call_site="ego_cycle",
                    content_preview=self._static_prompt[:200],
                )
                self._prompt_version_recorded = True
            except Exception:
                logger.debug("Failed to record ego prompt version", exc_info=True)
        system_prompt = self._static_prompt

        # Build invocation — ephemeral (no resume)
        invocation = CCInvocation(
            prompt=user_prompt,
            model=model,
            effort=effort,
            resume_session_id=None,
            append_system_prompt=True,
            system_prompt=system_prompt,
            timeout_s=2400,
            skip_permissions=True,
            working_dir=background_session_dir(),
            mcp_config=self._mcp_config_path,
        )

        # Autonomous dispatch check
        output = None
        session_id: str | None = None
        if self._autonomous_dispatcher is not None:
            decision = await self._autonomous_dispatcher.route(
                AutonomousDispatchRequest(
                    subsystem="ego",
                    policy_id=self._source_tag,
                    action_label=self._source_tag.replace("_", " "),
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    cli_invocation=invocation,
                    dispatch_mode="cli",
                    cli_fallback_allowed=True,
                    approval_required_for_cli=True,
                    approval_key_stable=True,
                ),
            )
            if decision.mode == "blocked":
                logger.warning("Ego unified cycle blocked: %s", decision.reason)
                raise CycleBlockedError(decision.reason or "approval pending")

        if output is None:
            try:
                sess = await self._session_manager.create_background(
                    session_type=SessionType.BACKGROUND_TASK,
                    model=model,
                    effort=effort,
                    source_tag=self._source_tag,
                )
                session_id = sess["id"]
                _set_obs_session(session_id)
            except Exception:
                logger.error(
                    "Failed to create ego background session (unified)",
                    exc_info=True,
                )
                return None

            try:
                output = await self._invoker.run(invocation)
            except Exception:
                logger.error("Ego CC invocation failed (unified)", exc_info=True)
                try:
                    await self._session_manager.fail(
                        session_id, reason="CC invocation error",
                    )
                except Exception:
                    logger.error("Session fail() also errored", exc_info=True)
                return None

        if output.is_error:
            logger.error(
                "Ego CC session returned error (unified): %s",
                output.error_message,
            )
            if session_id is not None:
                try:
                    await self._session_manager.fail(
                        session_id, reason=output.error_message,
                    )
                except Exception:
                    logger.error("Session fail() also errored", exc_info=True)
            return None

        # 3. ACT — shared output processing
        cycle = await self._process_cycle_output(output, model, session_id)

        # 4. LEARN — record cycle outcome
        await self._record_cycle_outcome(cycle, focus)

        # 5. Goal review post-processing: surface status recommendation
        if focus.focus_type == "goal_review" and focus.focus_id:
            try:
                parsed_output = self._parse_output(cycle.output_text)
                if parsed_output:
                    goal_rec = parsed_output.get("goal_status_recommendation")
                    if goal_rec and goal_rec != "continue":
                        await self._surface_goal_recommendation(
                            goal_id=focus.focus_id,
                            recommendation=goal_rec,
                            assessment=parsed_output.get(
                                "goal_assessment", ""
                            ),
                        )
            except Exception:
                logger.warning(
                    "Goal recommendation post-processing failed",
                    exc_info=True,
                )

        return cycle

    async def _perceive(self, signals: list[EgoSignal]) -> FocusResult | None:
        """Focus selection — perceive phase of the unified cognitive loop.

        Returns a FocusResult or None if no actionable signals.
        """
        from genesis.ego.focus import FocusResult, FocusSelector

        if not signals:
            return None

        # Lazy-init focus selector
        if self._focus_selector is None:
            if self._router is not None:
                self._focus_selector = FocusSelector(self._router)
            else:
                # No router → always return highest-priority signal directly.
                # This is the expected state in PR 1 (router wired in PR 2).
                sig = signals[0]
                weights = FocusSelector.get_context_weights(sig.focus_category)
                return FocusResult(
                    focus_type=sig.focus_category,
                    focus_id=sig.focus_id,
                    rationale="direct selection (no router available)",
                    signals_consumed=[s.id for s in signals],
                    context_weights=weights,
                )

        # Fetch recent focuses from ego_cycle_outcomes for context
        recent_focuses: list[dict[str, str]] = []
        try:
            rows = await ego_crud.list_cycle_outcomes(
                self._db, limit=5,
            )
            recent_focuses = [
                {
                    "focus_type": r.get("focus_type", ""),
                    "focus_id": r.get("focus_id", ""),
                    "rationale": r.get("perception_rationale", ""),
                    "created_at": r.get("created_at", ""),
                }
                for r in rows
            ]
        except Exception:
            # Table may not exist yet (migration not applied),
            # or no data yet. Both are fine.
            logger.debug("No recent focuses available", exc_info=True)

        return await self._focus_selector.select(signals, recent_focuses)

    async def _record_cycle_outcome(
        self,
        cycle: EgoCycle,
        focus: FocusResult,
    ) -> None:
        """Record cycle outcome for the Learn phase (ego_cycle_outcomes table)."""
        try:
            proposals = json.loads(cycle.proposals_json) if cycle.proposals_json else []
            # GROUNDWORK(unified-loop-dispatches): num_dispatches is always 0
            # here because dispatch count isn't known at cycle-completion time.
            # PR 2+ should wire the on_end hook to update this column after
            # dispatched sessions complete.
            await ego_crud.create_cycle_outcome(
                self._db,
                cycle_id=cycle.id,
                focus_type=focus.focus_type,
                focus_id=focus.focus_id,
                num_proposals=len(proposals),
                signals_consumed=json.dumps(focus.signals_consumed),
                perception_rationale=focus.rationale,
                perceive_cost_usd=focus.perceive_cost_usd,
                assessment=getattr(self, "_last_goal_assessment", None),
            )
        except Exception:
            logger.warning(
                "Failed to record cycle outcome for %s",
                cycle.id,
                exc_info=True,
            )

    async def _surface_goal_recommendation(
        self,
        *,
        goal_id: str,
        recommendation: str,
        assessment: str,
    ) -> None:
        """Surface a goal status recommendation as an observation.

        The ego assesses; the user decides. Recommendations appear in the
        user's morning report and may trigger Telegram notifications via
        the outreach pipeline.
        """
        import uuid

        from genesis.db.crud import observations as obs_crud

        try:
            from genesis.db.crud import user_goals

            goal = await user_goals.get_by_id(self._db, goal_id)
            title = (goal.get("title") or "?") if goal else "?"
            content = (
                f"Goal review recommendation for '{title}': "
                f"**{recommendation}**\n\n"
                f"Assessment: {assessment[:500]}"
            )
            await obs_crud.create(
                self._db,
                id=str(uuid.uuid4()),
                source="user_ego",
                type="goal_recommendation",
                content=content,
                priority="medium",
                category="goal_review",
                created_at=datetime.now(UTC).isoformat(),
            )
            logger.info(
                "Goal recommendation surfaced: %s → %s",
                goal_id[:12],
                recommendation,
            )
        except Exception:
            logger.warning(
                "Failed to surface goal recommendation", exc_info=True,
            )

    # -- Output processing --------------------------------------------------

    async def _process_cycle_output(
        self,
        output: object,
        model: CCModel,
        session_id: str | None,
    ) -> EgoCycle:
        """Post-invocation processing for ego cycles.

        Handles: parse → focus sanitize → store cycle → complete session →
        record last_run → realist gate → proposals → table/withdraw/unboard →
        execution briefs → follow-ups → directives → knowledge → escalations.

        Parameters
        ----------
        output:
            CC output object with .text, .cost_usd, .input_tokens, etc.
        model:
            CCModel used for the invocation (fallback for model_used).
        session_id:
            Background session ID, or None if dispatched via autonomous route.
        """
        # 6. Parse output
        parsed = self._parse_output(output.text)

        # 6b. (Removed) Focus sanitization no longer needed — focus_summary
        # is system-computed from DB state, not ego-authored. The ego's
        # output focus is logged in ego_cycles for audit but not persisted
        # to ego_state. See computed_focus.py.

        # 7. Store cycle (realist cost added below after _filter_proposals)
        self._last_realist_cost_usd = 0.0
        focus = parsed.get("focus_summary", "") if parsed else ""
        proposals_json = json.dumps(parsed.get("proposals", [])) if parsed else "[]"
        cycle = EgoCycle(
            output_text=output.text,
            proposals_json=proposals_json,
            focus_summary=focus,
            model_used=output.model_used or model.value,
            cost_usd=output.cost_usd,
            input_tokens=output.input_tokens,
            output_tokens=output.output_tokens,
            duration_ms=output.duration_ms,
            ego_source=self._source_tag,
        )
        await self._compaction.store_cycle(cycle)

        if session_id is not None:
            await self._session_manager.complete(
                session_id,
                cost_usd=output.cost_usd,
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
            )

            # 8. Record last run for neural monitor
            try:
                from genesis.observability.call_site_recorder import record_last_run

                await record_last_run(
                    self._db,
                    self._call_site,
                    provider="cc",
                    model_id=output.model_used or model.value,
                    response_text=output.text[:500] if output.text else "",
                    input_tokens=output.input_tokens,
                    output_tokens=output.output_tokens,
                )
            except Exception:
                logger.warning("Failed to record ego last_run", exc_info=True)

        # 8b. Extract goal_assessment for Learn phase (goal_review cycles).
        # Stored in ego_cycle_outcomes.assessment by _record_cycle_outcome().
        self._last_goal_assessment = (
            parsed.get("goal_assessment") if parsed else None
        )

        # 9. Process proposals
        if parsed:
            proposals = parsed.get("proposals", [])
            # communication_decision is intentionally per-cycle and NOT
            # persisted to ego_state. It controls THIS cycle's delivery only.
            comm_decision = parsed.get("communication_decision", "send_digest")
            if proposals:
                # Bypass realist gate when critical directives are active —
                # the user explicitly told the ego to propose something.
                # The realist's zombie detection would incorrectly block
                # re-proposals that were rejected for fixable reasons.
                has_critical_directive = False
                try:
                    active_dirs = await ego_crud.list_active_directives(
                        self._db, self._source_tag.replace("_cycle", ""),
                    )
                    has_critical_directive = any(
                        d.get("priority") == "critical"
                        for d in active_dirs
                    )
                except Exception:
                    pass
                if has_critical_directive:
                    logger.info(
                        "Realist bypassed — active critical directive(s)",
                    )
                else:
                    proposals = await self._filter_proposals(proposals)
                # Log realist cost for observability (cycle dataclass is frozen,
                # so cost is tracked via logging; negligible vs ego cycle cost)
                if self._last_realist_cost_usd > 0:
                    logger.info(
                        "Realist cost: $%.4f (ego cycle: $%.4f)",
                        self._last_realist_cost_usd,
                        cycle.cost_usd,
                    )
                # Domain enforcement: Genesis ego can only propose on
                # infrastructure/operations. User-domain proposals
                # (content, career, etc.) are rejected and auto-escalated.
                if proposals and self._source_tag == "genesis_ego_cycle":
                    proposals = self._enforce_domain_boundary(proposals)
                elif proposals and self._source_tag == "user_ego_cycle":
                    proposals = await self._enforce_user_domain_boundary(proposals)

                if proposals:
                    await self._process_proposals(
                        proposals,
                        cycle.id,
                        communication_decision=comm_decision,
                    )

            # 9a. Process notifications (informational, no approval gate)
            notifications = parsed.get("notifications", [])
            if isinstance(notifications, list) and notifications:
                await self._process_notifications(notifications)

            # 9b. Process tabled/withdrawn proposal IDs
            tabled_ids = parsed.get("tabled", [])
            if isinstance(tabled_ids, list):
                for pid in tabled_ids:
                    if isinstance(pid, str) and pid:
                        # 24h guard: don't table proposals delivered < 24h ago
                        prop = await ego_crud.get_proposal(self._db, pid)
                        if prop:
                            created = prop.get("created_at", "")
                            if created:
                                try:
                                    age = datetime.now(UTC) - datetime.fromisoformat(created)
                                    if age.total_seconds() < 86400:  # 24 hours
                                        logger.info(
                                            "Proposal %s tabling blocked (%.1fh old, <24h guard)",
                                            pid, age.total_seconds() / 3600,
                                        )
                                        continue
                                except (ValueError, TypeError):
                                    pass  # Unparseable timestamp — allow tabling

                        ok = await ego_crud.table_proposal(self._db, pid)
                        if ok:
                            logger.info("Proposal %s tabled by ego", pid)
                            try:
                                from genesis.db.crud import intervention_journal as journal_crud

                                await journal_crud.resolve(
                                    self._db,
                                    pid,
                                    outcome_status="tabled",
                                )
                            except Exception:
                                pass

            withdrawn_ids = parsed.get("withdrawn", [])
            if isinstance(withdrawn_ids, list):
                for pid in withdrawn_ids:
                    if isinstance(pid, str) and pid:
                        # 24h guard: don't withdraw proposals delivered < 24h ago
                        prop = await ego_crud.get_proposal(self._db, pid)
                        if prop:
                            created = prop.get("created_at", "")
                            if created:
                                try:
                                    age = datetime.now(UTC) - datetime.fromisoformat(created)
                                    if age.total_seconds() < 86400:  # 24 hours
                                        logger.info(
                                            "Proposal %s withdrawal blocked (%.1fh old, <24h guard)",
                                            pid, age.total_seconds() / 3600,
                                        )
                                        continue
                                except (ValueError, TypeError):
                                    pass  # Unparseable timestamp — allow withdrawal

                        ok = await ego_crud.withdraw_proposal(self._db, pid)
                        if ok:
                            logger.info("Proposal %s withdrawn by ego", pid)
                            try:
                                from genesis.db.crud import intervention_journal as journal_crud

                                await journal_crud.resolve(
                                    self._db,
                                    pid,
                                    outcome_status="withdrawn",
                                )
                            except Exception:
                                pass

            # 9b-2. Process unboarded proposals (remove from board, keep pending)
            unboarded_ids = parsed.get("unboarded", [])
            if isinstance(unboarded_ids, list):
                for pid in unboarded_ids:
                    if isinstance(pid, str) and pid:
                        ok = await ego_crud.unboard_proposal(self._db, pid)
                        if ok:
                            logger.info(
                                "Proposal %s unboarded by ego (remains pending)",
                                pid,
                            )

            # 9c. Process execution briefs (ego-as-executor)
            execution_briefs = parsed.get("execution_briefs", [])
            if isinstance(execution_briefs, list) and execution_briefs:
                await self._process_execution_briefs(execution_briefs)

            # 10. Record follow_ups (deduped against existing pending)
            follow_ups = parsed.get("follow_ups", [])
            if follow_ups:
                await self._dispatcher.record_follow_ups(follow_ups, cycle.id)

            # 10b. Resolve follow_ups the ego marked as done
            resolved_follow_ups = parsed.get("resolved_follow_ups", [])
            if isinstance(resolved_follow_ups, list) and resolved_follow_ups:
                await self._dispatcher.resolve_follow_ups(
                    resolved_follow_ups,
                    cycle.id,
                )

            # 10c. Resolve directives the ego addressed
            resolved_directives = parsed.get("resolved_directives", [])
            if isinstance(resolved_directives, list):
                for rd in resolved_directives:
                    if isinstance(rd, dict) and "id" in rd:
                        try:
                            await ego_crud.resolve_directive(
                                self._db, rd["id"],
                                status="completed",
                                resolution=rd.get("resolution", ""),
                            )
                            logger.info(
                                "Directive %s resolved by ego", rd["id"],
                            )
                        except Exception:
                            logger.debug(
                                "Failed to resolve directive %s",
                                rd.get("id"), exc_info=True,
                            )

            # 10d. Process deferred intentions (both egos)
            intentions_data = parsed.get("intentions")
            if intentions_data and isinstance(intentions_data, dict):
                await self._process_intentions(intentions_data)

            # 11. Compute and store factual focus summary.
            # The ego's authored focus is already logged in ego_cycles
            # (step 7 above). We compute a DB-derived factual summary
            # to prevent self-reinforcing behavioral loops.
            try:
                from genesis.ego.computed_focus import compute_focus_summary

                computed = await compute_focus_summary(
                    self._db, self._focus_summary_key,
                )
                await ego_crud.set_state(
                    self._db,
                    key=self._focus_summary_key,
                    value=computed,
                )
            except Exception:
                logger.warning(
                    "Failed to compute focus summary", exc_info=True,
                )

            # 12. Process escalations (Genesis ego → observations for user ego)
            escalations = parsed.get("escalations", [])
            if isinstance(escalations, list) and escalations:
                await self._process_escalations(escalations, cycle.id)
        else:
            logger.warning(
                "Ego output could not be parsed — cycle %s stored with no proposals",
                cycle.id,
            )

        logger.info(
            "Ego cycle %s completed (cost=$%.4f, proposals=%d, tokens=%d+%d)",
            cycle.id,
            output.cost_usd,
            len(parsed.get("proposals", [])) if parsed else 0,
            output.input_tokens,
            output.output_tokens,
        )
        return cycle

    # -- Prompt building ---------------------------------------------------

    def _build_focused_prompt(
        self,
        *,
        dynamic_context: str,
        focus: FocusResult,
    ) -> str:
        """Build a focus-specific user message for ego cycles.

        The system prompt is the static identity only (cacheable).
        All dynamic content goes here in the user message.  The focus
        directive comes from the perceive phase (FocusSelector output).

        Parameters
        ----------
        dynamic_context:
            Assembled operational context from compaction engine.
        focus:
            FocusResult with focus_type, focus_id, rationale.
        """
        directive = (
            f"Your focus this cycle: **{focus.focus_type}** — {focus.rationale}\n\n"
            "Review the operational context below with this focus in mind. "
            "Check your open threads and use your MCP tools to verify "
            "any beliefs before proposing actions. End with valid JSON "
            "matching the ego output schema."
        )

        # Focus-specific instructions
        if focus.focus_type == "daily_briefing":
            directive += (
                "\n\nThis is a DAILY BRIEFING cycle. Include the morning_report "
                "field with your daily briefing for the user."
            )
        elif focus.focus_type == "goal_review":
            directive += (
                "\n\nThis is a GOAL REVIEW cycle. Assess progress on the "
                "focused goal, identify blockers, and propose goal-advancing "
                "actions. If this goal is too broad, suggest specific subgoals "
                "in your goal_assessment that the user can create. Include "
                "the goal_assessment field with your analysis of the goal's "
                "current state. Include goal_status_recommendation: one of "
                '"continue", "pause", "deprioritize", or "close".'
            )
        elif focus.focus_type == "reactive":
            directive += (
                "\n\nThis is a REACTIVE cycle. Respond to the event(s) that "
                "triggered this cycle."
            )
        elif focus.focus_type == "dispatch_outcome":
            directive += (
                "\n\nThis cycle was triggered by a dispatch outcome. "
                "Assess the result and determine next steps."
            )
        elif focus.focus_type == "escalation":
            directive += (
                "\n\nThis is an ESCALATION cycle. Assess the health issue "
                "or system alert and propose remediation actions."
            )

        return f"{directive}\n\n---\n\n{dynamic_context}"

    # -- Output parsing ----------------------------------------------------

    @staticmethod
    def _parse_output(raw_text: str) -> dict | None:
        """Extract structured JSON from ego output.

        Three-step fallback:
        1. Direct ``json.loads()``
        2. Extract from markdown code block (```json ... ```)
        3. Find first ``{`` to last ``}`` and parse

        Returns parsed dict or None on failure.
        """
        if not raw_text or not raw_text.strip():
            return None

        text = raw_text.strip()

        # Step 1: direct parse
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return _validate_output(result)
        except json.JSONDecodeError:
            pass

        # Step 2: markdown code block
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if match:
            try:
                result = json.loads(match.group(1).strip())
                if isinstance(result, dict):
                    return _validate_output(result)
            except json.JSONDecodeError:
                pass

        # Step 3: brace extraction
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            try:
                result = json.loads(text[first_brace : last_brace + 1])
                if isinstance(result, dict):
                    return _validate_output(result)
            except json.JSONDecodeError:
                pass

        logger.error(
            "Failed to parse ego output (length=%d): %.200s...",
            len(text),
            text,
        )
        return None

    # -- Helpers -----------------------------------------------------------

    async def _filter_proposals(
        self,
        proposals: list[dict],
    ) -> list[dict]:
        """Realist gate — LLM evaluates proposals against recent history.

        The dreamer proposes freely; the realist catches:
        1. Read-only investigations disguised as proposals (investigate is free)
        2. Zombie proposals (same topic proposed + withdrawn/tabled/expired)
        3. Infeasible proposals (requires capabilities Genesis doesn't have)
        4. Vague proposals that need amendment with concrete steps

        Annotations are stored on proposals that pass (via _realist_verdict
        and _realist_reasoning keys). The ego sees these in its next cycle's
        proposal history context, forming the outer dreamer→realist loop.

        Gracefully degrades to pass-through on ANY failure (DB, CC, parse).
        """
        if not proposals:
            return proposals

        # Fetch recent history for zombie/duplicate detection
        try:
            cursor = await self._db.execute(
                "SELECT action_type, content, status, created_at "
                "FROM ego_proposals "
                "WHERE created_at >= datetime('now', '-2 days') "
                "ORDER BY created_at DESC LIMIT 20",
            )
            recent = [dict(r) for r in await cursor.fetchall()]
        except Exception:
            logger.warning("Realist: failed to fetch history, passing through")
            return proposals

        prompt = _build_realist_prompt(proposals, recent, ego_source=self._source_tag)

        try:
            invocation = CCInvocation(
                prompt=prompt,
                model=CCModel.OPUS,
                effort=EffortLevel.MEDIUM,
                skip_permissions=True,
                working_dir=background_session_dir(),
            )
            output = await self._invoker.run(invocation)
            # Track realist cost for cycle accounting
            self._last_realist_cost_usd = output.cost_usd
            if output.is_error:
                logger.warning("Realist CC call failed: %s", output.error_message)
                return proposals

            verdicts = _parse_realist_response(output.text, len(proposals))

            filtered = []
            rejected_count = 0
            amended_count = 0
            for i, prop in enumerate(proposals):
                verdict = verdicts.get(i, {"verdict": "pass", "reasoning": ""})
                prop["_realist_verdict"] = verdict["verdict"]
                prop["_realist_reasoning"] = verdict.get("reasoning", "")

                if verdict["verdict"] == "amend" and verdict.get("amended_content"):
                    prop["_original_content"] = prop["content"]
                    prop["content"] = verdict["amended_content"]
                    amended_count += 1

                if verdict["verdict"] != "reject":
                    filtered.append(prop)
                else:
                    rejected_count += 1
                    logger.info(
                        "Realist rejected: %s — %s",
                        prop.get("content", "")[:80],
                        verdict.get("reasoning", "")[:100],
                    )
                    # Create redirect observation for genesis ego so it
                    # picks up the topic in its next cycle. Any realist
                    # rejection of a genesis ego proposal creates a
                    # redirect — the wording of the rejection may vary.
                    if self._source_tag == "genesis_ego_cycle":
                        with contextlib.suppress(Exception):
                            await self._create_redirect_observation(
                                prop,
                                redirect_type="realist_redirect",
                            )

            if rejected_count or amended_count:
                logger.info(
                    "Realist: %d/%d passed (%d rejected, %d amended)",
                    len(filtered),
                    len(proposals),
                    rejected_count,
                    amended_count,
                )

        except Exception:
            logger.warning("Realist filter failed, passing through", exc_info=True)
            filtered = proposals  # fail-open: use unfiltered list

        # Quality gate (Verified Autonomy L3) — runs AFTER realist gate.
        # Placed outside the realist try-except so a quality gate failure
        # cannot accidentally undo realist rejections. _quality_gate has
        # its own fail-open error handling.
        filtered = await self._quality_gate(filtered)
        return filtered

    async def _quality_gate(self, proposals: list[dict]) -> list[dict]:
        """Score proposals for coherence/relevance/completeness.

        Proposals below threshold get ``_realist_verdict = "quality_hold"``
        and stay in the list (stored in DB) but ``send_digest()`` skips them.
        Fails open: any error passes all proposals through.
        """
        if not proposals or not self._router:
            return proposals

        try:
            from genesis.eval.scorers import get_scorer
            from genesis.eval.types import ScorerType

            scorer = get_scorer(ScorerType.OUTPUT_QUALITY)
            scorer.set_router(self._router)

            held_count = 0
            for p in proposals:
                try:
                    passed, score, detail = await scorer.score_async(
                        actual=(
                            f"{p.get('content', '')}\n\n"
                            f"Rationale: {p.get('rationale', '')}"
                        ),
                        expected="autonomous proposal",
                        config={"rubric_name": "output_quality"},
                    )
                    if not passed:
                        p["_realist_verdict"] = "quality_hold"
                        p["_realist_reasoning"] = (
                            f"Quality score {score:.2f} below threshold. {detail[:300]}"
                        )
                        held_count += 1
                except Exception:
                    logger.debug("Quality scoring failed for proposal, passing", exc_info=True)

            if held_count:
                logger.info(
                    "Quality gate: held %d/%d proposals", held_count, len(proposals),
                )
        except Exception:
            logger.warning("Quality gate failed, passing all proposals", exc_info=True)

        return proposals

    # Genesis ego allowed action categories (infrastructure/operations only)
    _GENESIS_EGO_ALLOWED_CATEGORIES = frozenset({
        "system_health", "infrastructure", "performance",
        "maintenance", "security", "cost_protection",
        "system_monitoring", "genesis_maintenance",
    })

    def _enforce_domain_boundary(
        self,
        proposals: list[dict],
    ) -> list[dict]:
        """Filter out-of-domain proposals from the Genesis ego.

        The Genesis ego (COO) may only propose on infrastructure and
        operations. User-domain proposals (content, career, communication)
        are logged as domain violations and dropped. The ego's prompt
        already states this boundary; this is the architectural backstop.
        """
        allowed = []
        for p in proposals:
            category = p.get("action_category", "")
            if category in self._GENESIS_EGO_ALLOWED_CATEGORIES:
                allowed.append(p)
            else:
                logger.warning(
                    "Genesis ego domain violation: proposal with "
                    "action_category=%r dropped (content: %s)",
                    category,
                    p.get("content", "")[:80],
                )
        return allowed

    async def _enforce_user_domain_boundary(
        self,
        proposals: list[dict],
    ) -> list[dict]:
        """Redirect infrastructure proposals from the user ego.

        The user ego (CEO) should not propose on infrastructure domains.
        When it does, the proposal is redirected as an observation for the
        genesis ego to pick up, preserving the signal without polluting
        the user ego's proposal queue.
        """
        allowed = []
        for p in proposals:
            category = p.get("action_category", "")
            if _normalize_to_infra(category):
                logger.info(
                    "User ego infra redirect: category=%r → observation (content: %s)",
                    category,
                    p.get("content", "")[:80],
                )
                with contextlib.suppress(Exception):
                    await self._create_redirect_observation(
                        p, redirect_type="cross_domain_redirect",
                    )
            else:
                allowed.append(p)
        return allowed

    async def _create_redirect_observation(
        self,
        proposal: dict,
        *,
        redirect_type: str = "cross_domain_redirect",
    ) -> None:
        """Create a redirect observation for a cross-domain proposal.

        Uses dedup (skip_if_duplicate) and 3-day TTL so redirects
        auto-expire if the target ego doesn't act on them.
        """
        try:
            from genesis.db.crud import observations as obs_crud

            content = (
                f"Redirected from {self._source_tag}: "
                f"{proposal.get('content', '')[:300]}"
            )
            expires = (datetime.now(UTC) + timedelta(days=3)).isoformat()
            await obs_crud.create(
                self._db,
                id=uuid.uuid4().hex,
                source=f"ego_domain_redirect:{self._source_tag}",
                type=redirect_type,
                content=content,
                # Map proposal urgency to observation priority.
                # Proposal uses "normal"; observations use "medium".
                priority={"normal": "medium"}.get(
                    proposal.get("urgency", "medium"),
                    proposal.get("urgency", "medium"),
                ),
                created_at=datetime.now(UTC).isoformat(),
                category="infrastructure",
                expires_at=expires,
                skip_if_duplicate=True,
            )
        except Exception:
            logger.warning(
                "Failed to create redirect observation", exc_info=True,
            )

    async def _process_proposals(
        self,
        proposals: list[dict],
        cycle_id: str,
        *,
        communication_decision: str = "send_digest",
    ) -> None:
        """Create proposal batch, optionally send to Telegram.

        The ego's ``communication_decision`` gates delivery:
        - ``send_digest`` / ``urgent_notify``: create batch + send
        - ``stay_quiet``: create batch only (proposals stored, not sent)
        """
        try:
            # Auto-table oldest unranked proposals when queue exceeds cap.
            # Respects the 24h guard — proposals < 24h old are not tabled.
            try:
                pending = await ego_crud.list_pending_proposals(
                    self._db, ego_source=self._source_tag,
                )
                max_pending = getattr(self._config, "max_pending_proposals", 15)
                if len(pending) + len(proposals) > max_pending:
                    unranked = [
                        p for p in pending if p.get("rank") is None
                    ]
                    excess = len(pending) + len(proposals) - max_pending
                    oldest = sorted(
                        unranked, key=lambda x: x.get("created_at", ""),
                    )
                    for p in oldest[:excess]:
                        created = p.get("created_at", "")
                        if created:
                            try:
                                age = datetime.now(UTC) - datetime.fromisoformat(created)
                                if age.total_seconds() < 86400:
                                    continue  # 24h guard
                            except (ValueError, TypeError):
                                pass
                        await ego_crud.table_proposal(self._db, p["id"])
                        logger.info(
                            "Auto-tabled proposal %s (queue cap %d)",
                            p["id"][:12], max_pending,
                        )
            except Exception:
                logger.debug("Pending cap check failed, proceeding", exc_info=True)

            batch_id, ids = await self._proposals.create_batch(
                proposals,
                cycle_id=cycle_id,
                ego_source=self._source_tag,
            )
            logger.info(
                "Created proposal batch %s with %d proposals",
                batch_id,
                len(ids),
            )

            # Record intervention journal entries (fire-and-forget)
            try:
                from genesis.db.crud import intervention_journal as journal_crud

                now = datetime.now(UTC).isoformat()
                for pid, prop in zip(ids, proposals, strict=False):
                    await journal_crud.create(
                        self._db,
                        ego_source=self._source_tag,
                        proposal_id=pid,
                        cycle_id=cycle_id,
                        action_type=prop.get("action_type", "unknown"),
                        action_summary=prop.get("content", "")[:500],
                        expected_outcome=prop.get("rationale", ""),
                        confidence=prop.get("confidence", 0.0),
                        created_at=now,
                    )
            except Exception:
                logger.warning("Failed to create intervention journal entries", exc_info=True)

            # Structural validation — annotates digest, doesn't block
            validation_issues = await self._proposals.validate_batch(proposals)
            if validation_issues:
                logger.warning(
                    "Proposal validation issues in batch %s: %s",
                    batch_id,
                    "; ".join(validation_issues),
                )

            if communication_decision in ("send_digest", "urgent_notify"):
                delivery = await self._proposals.send_digest(
                    batch_id,
                    validation_warnings=validation_issues or None,
                    ego_source=self._source_tag,
                )
                if delivery:
                    logger.info("Ego digest sent (delivery_id=%s)", delivery)
            else:
                logger.info(
                    "Ego decided stay_quiet — batch %s stored only",
                    batch_id,
                )
        except Exception:
            logger.error("Failed to process ego proposals", exc_info=True)

    async def _process_execution_briefs(
        self,
        briefs: list[dict],
    ) -> None:
        """Dispatch approved proposals via DirectSessionRunner.

        The ego outputs execution_briefs referencing approved proposal IDs.
        For each brief, we verify the proposal is actually approved, then
        spawn a background session. On success the proposal transitions to
        'executed'; on failure it transitions to 'failed'.
        """
        if self._direct_session_runner is None:
            logger.warning("No DirectSessionRunner — cannot dispatch execution briefs")
            return

        from genesis.cc.direct_session import VALID_PROFILES, DirectSessionRequest
        from genesis.cc.types import CCModel, EffortLevel

        for brief in briefs:
            if not isinstance(brief, dict):
                continue
            proposal_id = brief.get("proposal_id", "")
            prompt = brief.get("prompt", "")
            if not proposal_id or not prompt:
                continue

            # Append content firewall rules to ego-authored dispatch prompt.
            # The ego writes the brief prompt directly — this safety net
            # catches information that slips through the ego's own judgment.
            prompt = f"{prompt}\n\n{_CONTENT_FIREWALL_RULES}"

            # Map profile and model from brief
            profile = brief.get("profile", "observe")
            if profile not in VALID_PROFILES:
                profile = "observe"
            model_str = brief.get("model", "sonnet")
            if model_str == "opus":
                model = CCModel.OPUS
            elif model_str == "haiku":
                model = CCModel.HAIKU
            else:
                model = CCModel.SONNET

            # Autonomy dispatch gate — same check as sweep_approved_inner.
            # Load full proposal to evaluate action domain against current
            # autonomy level. Gate fires BEFORE the atomic claim so blocked
            # proposals stay 'approved' (ego-invisible).
            if self._proposal_gate is not None:
                try:
                    prop_row = await ego_crud.get_proposal(self._db, proposal_id)
                    if prop_row is None:
                        # Proposal disappeared — skip (don't dispatch ungated)
                        logger.warning(
                            "Execution brief %s: proposal not found — skipping",
                            proposal_id,
                        )
                        continue
                    decision = await self._proposal_gate.evaluate(prop_row)
                    if not decision.allowed:
                            logger.info(
                                "Execution brief %s blocked by dispatch gate: %s (domain=%s)",
                                proposal_id,
                                decision.reason,
                                decision.action_domain,
                            )
                            if self._event_bus:
                                from genesis.observability.types import Severity, Subsystem
                                await self._event_bus.emit(
                                    Subsystem.AUTONOMY,
                                    Severity.INFO,
                                    "autonomy.dispatch_gate.block",
                                    f"Blocked execution brief: {decision.reason}",
                                    proposal_id=proposal_id,
                                    action_domain=str(decision.action_domain),
                                    rule_id=decision.rule_id,
                                )
                            continue
                except Exception:
                    logger.error(
                        "Proposal gate failed for execution brief %s — allowing dispatch",
                        proposal_id,
                        exc_info=True,
                    )

            # Atomically claim the proposal BEFORE spawning to prevent
            # double-dispatch (sweep_approved_proposals is a second path).
            # Use raw SQL to preserve resolved_at (the approval timestamp
            # the staleness guard depends on).
            cursor = await self._db.execute(
                "UPDATE ego_proposals SET status = 'executed', "
                "user_response = 'dispatching' "
                "WHERE id = ? AND status = 'approved'",
                (proposal_id,),
            )
            await self._db.commit()
            if cursor.rowcount == 0:
                logger.info(
                    "Execution brief for proposal %s skipped — already claimed",
                    proposal_id,
                )
                continue

            try:
                request = DirectSessionRequest(
                    prompt=prompt,
                    profile=profile,
                    model=model,
                    effort=EffortLevel.HIGH,
                    notify=True,
                    source_tag="ego_dispatch",
                    caller_context=f"ego_proposal:{proposal_id}",
                )
                session_id = await self._direct_session_runner.spawn(request)
                # Update with actual session ID
                await self._db.execute(
                    "UPDATE ego_proposals SET user_response = ? WHERE id = ?",
                    (f"session:{session_id}", proposal_id),
                )
                await self._db.commit()
                try:
                    from genesis.db.crud import intervention_journal as journal_crud

                    await journal_crud.resolve(
                        self._db,
                        proposal_id,
                        outcome_status="executed",
                        actual_outcome=f"Dispatched as session:{session_id}",
                    )
                except Exception:
                    logger.warning("Journal resolve failed for %s", proposal_id)
                logger.info(
                    "Dispatched proposal %s → session %s",
                    proposal_id,
                    session_id,
                )
            except Exception:
                logger.error(
                    "Failed to dispatch proposal %s — reverting to approved",
                    proposal_id,
                    exc_info=True,
                )
                try:
                    await self._db.execute(
                        "UPDATE ego_proposals SET status = 'approved', "
                        "user_response = NULL WHERE id = ?",
                        (proposal_id,),
                    )
                    await self._db.commit()
                except Exception:
                    logger.error(
                        "Failed to revert proposal %s — stuck at executed",
                        proposal_id,
                        exc_info=True,
                    )
                try:
                    from genesis.db.crud import intervention_journal as journal_crud

                    await journal_crud.resolve(
                        self._db,
                        proposal_id,
                        outcome_status="failed",
                        actual_outcome="Dispatch failed",
                    )
                except Exception:
                    logger.warning("Journal resolve failed for %s", proposal_id)

    async def _process_escalations(
        self,
        escalations: list[dict],
        cycle_id: str,
    ) -> None:
        """Write escalations as observations for the user ego to see.

        Only the Genesis ego produces escalations. Each escalation becomes
        an observation with type='escalation_to_user_ego' so the user ego
        context builder can query and display them.

        Deduplication: before creating a new escalation, check if an
        unresolved one with similar content exists in the last 24 hours.
        This prevents the Genesis ego from re-escalating the same issue
        every cycle (e.g., 3 "backup failing" escalations in 30 minutes).
        """
        import uuid

        from genesis.db.crud import observations as obs_crud

        # Fetch recent unresolved escalations for dedup (24h window)
        try:
            cursor = await self._db.execute(
                "SELECT content FROM observations "
                "WHERE type = 'escalation_to_user_ego' "
                "AND resolved_at IS NULL "
                "AND created_at > datetime('now', '-24 hours')"
            )
            recent_contents = [
                row[0].lower()[:100] for row in await cursor.fetchall()
            ]
        except Exception:
            recent_contents = []  # Fail open — allow all escalations

        created = 0
        deduped = 0
        for esc in escalations:
            if not isinstance(esc, dict):
                continue
            content_parts = [esc.get("content", "")]
            ctx = esc.get("context")
            if ctx:
                content_parts.append(f"Context: {ctx}")
            suggested = esc.get("suggested_action")
            if suggested:
                content_parts.append(f"Suggested: {suggested}")

            full_content = "\n".join(content_parts)

            # Dedup: check if the first 100 chars (lowered) of the main
            # content match any recent unresolved escalation.
            main_content_prefix = esc.get("content", "").lower()[:100]
            if any(
                main_content_prefix and existing.startswith(main_content_prefix[:50])
                for existing in recent_contents
            ):
                deduped += 1
                continue

            try:
                await obs_crud.create(
                    self._db,
                    id=str(uuid.uuid4()),
                    source="genesis_ego",
                    type="escalation_to_user_ego",
                    content=full_content,
                    priority="high",
                    created_at=datetime.now(UTC).isoformat(),
                    category="escalation",
                )
                # Add to recent for intra-batch dedup
                recent_contents.append(main_content_prefix)
                created += 1
            except Exception:
                logger.error(
                    "Failed to write escalation from cycle %s",
                    cycle_id,
                    exc_info=True,
                )

        if escalations:
            logger.info(
                "Genesis ego cycle %s: %d escalation(s) — %d created, %d deduped",
                cycle_id,
                len(escalations),
                created,
                deduped,
            )

    async def _process_notifications(self, notifications: list[dict]) -> None:
        """Submit ego notifications to the outreach pipeline.

        Notifications are informational messages that don't need user approval.
        They route through the outreach pipeline with NOTIFICATION category,
        subject to governance (dedup, rate limit, quiet hours) but exempt from
        salience threshold and engagement throttle.

        Fire-and-forget: errors are logged but never block the ego cycle.
        """
        if self._outreach_pipeline is None:
            logger.warning(
                "OutreachPipeline not available — skipping %d notification(s)",
                len(notifications),
            )
            return

        from genesis.outreach.types import OutreachCategory, OutreachRequest

        submitted = 0
        for notif in notifications:
            if not isinstance(notif, dict):
                continue
            content = notif.get("content", "").strip()
            if not content:
                continue
            # Cap content length to prevent runaway LLM output from
            # flooding the outreach pipeline with megabyte-sized payloads.
            if len(content) > 2000:
                content = content[:2000]

            # Map ego urgency to salience score so governance thresholds
            # can differentiate. NOTIFICATION threshold is 0.0 by default
            # so all pass, but this preserves the signal for future tuning.
            urgency = notif.get("urgency", "normal")
            salience = {"low": 0.3, "normal": 0.6, "high": 0.9}.get(urgency, 0.6)

            try:
                request = OutreachRequest(
                    category=OutreachCategory.NOTIFICATION,
                    topic=content[:100],
                    context=content,
                    salience_score=salience,
                    signal_type="ego_notification",
                    channel="telegram",
                )
                await self._outreach_pipeline.submit(request)
                submitted += 1
            except Exception:
                logger.warning(
                    "Failed to submit ego notification: %s",
                    content[:80],
                    exc_info=True,
                )

        if submitted:
            logger.info(
                "Submitted %d/%d ego notification(s) to outreach pipeline",
                submitted,
                len(notifications),
            )

    # -- Deferred intentions ------------------------------------------------

    async def _process_intentions(
        self,
        intentions_data: dict,
    ) -> None:
        """Process the ego's intentions output: review existing + create new.

        Actions in review: keep (increment cycle_count), fire (mark fired),
        withdraw (mark withdrawn), renew (reset cycle_count).
        """
        try:
            from genesis.db.crud import ego_intentions

            # 1. Auto-expire overdue intentions FIRST — clean the working set
            # before the ego's review actions take effect. Uses strict > so
            # an intention at exactly max_cycles survives one final review.
            expired = await ego_intentions.expire_overdue(
                self._db, self._source_tag,
            )
            if expired:
                logger.info(
                    "Auto-expired %d intention(s) for %s",
                    expired, self._source_tag,
                )

            # 2. Review existing intentions (filtered to this ego's source)
            reviews = intentions_data.get("review", [])
            if isinstance(reviews, list):
                for entry in reviews:
                    if not isinstance(entry, dict):
                        continue
                    iid = entry.get("id")
                    action = entry.get("action")
                    if not iid or action not in ("keep", "fire", "withdraw", "renew"):
                        continue

                    if action == "keep":
                        new_count = await ego_intentions.increment_cycle_count(
                            self._db, iid, ego_source=self._source_tag,
                        )
                        logger.debug("Intention %s kept (cycle %d)", iid, new_count)
                    elif action == "fire":
                        ok = await ego_intentions.fire(
                            self._db, iid, ego_source=self._source_tag,
                        )
                        if ok:
                            logger.info("Intention %s fired", iid)
                        else:
                            logger.warning("Intention %s fire failed (not active?)", iid)
                    elif action == "withdraw":
                        ok = await ego_intentions.withdraw(
                            self._db, iid, ego_source=self._source_tag,
                        )
                        if ok:
                            logger.info("Intention %s withdrawn", iid)
                    elif action == "renew":
                        ok = await ego_intentions.renew(
                            self._db, iid, ego_source=self._source_tag,
                        )
                        if ok:
                            logger.info("Intention %s renewed (counter reset)", iid)

            # 3. Create new intentions
            new_intentions = intentions_data.get("new", [])
            if isinstance(new_intentions, list):
                for item in new_intentions:
                    if not isinstance(item, dict):
                        continue
                    content = (item.get("content") or "").strip()[:500]
                    trigger = (item.get("trigger_condition") or "").strip()[:500]
                    if not content or not trigger:
                        logger.warning("Skipping intention with empty content/trigger")
                        continue

                    try:
                        max_cycles = min(int(item.get("max_cycles", 20)), 50)
                    except (ValueError, TypeError):
                        max_cycles = 20
                    priority = item.get("priority", "normal")
                    if priority not in ("low", "normal", "high"):
                        priority = "normal"

                    iid = await ego_intentions.create(
                        self._db,
                        content=content,
                        trigger_condition=trigger,
                        ego_source=self._source_tag,
                        reasoning=str(item.get("reasoning", ""))[:500],
                        priority=priority,
                        max_cycles=max_cycles,
                    )
                    if iid:
                        logger.info("Created intention %s for %s", iid, self._source_tag)
                    # None return means cap reached — already logged by CRUD

            await self._db.commit()
        except Exception:
            logger.error("Failed to process intentions", exc_info=True)

    # -- Approved proposal sweep --------------------------------------------

    async def sweep_approved_proposals(self) -> list[str]:
        """Mechanically dispatch approved proposals via DirectSessionRunner.

        Called on a fixed 30-minute interval by EgoCadenceManager AND
        after user approval via Telegram or dashboard (5-min grace).
        The sweep lock prevents concurrent execution (double-dispatch
        guard).

        Returns list of dispatched proposal IDs.
        """
        async with self._sweep_lock:
            return await self._sweep_approved_inner()

    async def _sweep_approved_inner(self) -> list[str]:
        """Inner sweep logic — must be called under self._sweep_lock."""
        if self._direct_session_runner is None:
            return []

        from genesis.cc.direct_session import DirectSessionRequest

        approved = await ego_crud.list_proposals(
            self._db,
            status="approved",
            limit=5,
        )
        if not approved:
            return []

        dispatched: list[str] = []
        for prop in approved:
            # Staleness guard — skip proposals approved more than 7 days ago.
            # Proposals go stale by clock only as a safety net; semantic
            # staleness (premise invalid) is the ego's job to evaluate.
            try:
                approved_at = datetime.fromisoformat(prop["resolved_at"])
                if datetime.now(UTC) - approved_at > timedelta(days=7):
                    continue
            except (KeyError, TypeError, ValueError):
                continue

            # Autonomy dispatch gate — silently skip proposals that exceed
            # the current autonomy level for their action domain.
            # The ego never learns about blocks; proposals stay 'approved'
            # and expire via the 7-day staleness guard naturally.
            if self._proposal_gate is not None:
                try:
                    decision = await self._proposal_gate.evaluate(prop)
                    if not decision.allowed:
                        logger.info(
                            "Proposal %s blocked by dispatch gate: %s (domain=%s)",
                            prop["id"],
                            decision.reason,
                            decision.action_domain,
                        )
                        # Emit event for observability (user-visible, ego-invisible)
                        if self._event_bus:
                            from genesis.observability.types import Severity, Subsystem
                            await self._event_bus.emit(
                                Subsystem.AUTONOMY,
                                Severity.INFO,
                                "autonomy.dispatch_gate.block",
                                f"Blocked proposal: {decision.reason}",
                                proposal_id=prop["id"],
                                action_type=prop.get("action_type"),
                                action_domain=str(decision.action_domain),
                                rule_id=decision.rule_id,
                            )
                        continue
                except Exception:
                    # Gate failure is non-fatal — default to allowing dispatch
                    # (fail-open, log the error for investigation)
                    logger.error(
                        "Proposal gate evaluation failed for %s — allowing dispatch",
                        prop["id"],
                        exc_info=True,
                    )

            # Content integrity check — detect degradation since creation.
            # Log-only for now; tighten to a gate if we see real degradation.
            stored_hash = prop.get("content_hash")
            stored_size = prop.get("content_size")
            if stored_hash is not None and stored_size is not None:
                from genesis.ego.integrity import content_hash as _chash
                from genesis.ego.integrity import content_size as _csize

                current_hash = _chash(prop["content"])
                current_size = _csize(prop["content"])
                if current_hash != stored_hash:
                    logger.warning(
                        "Proposal %s content hash mismatch — content may "
                        "have been modified since creation",
                        prop["id"],
                    )
                if stored_size > 0:
                    shrinkage = (stored_size - current_size) / stored_size * 100
                    if shrinkage > 15:
                        logger.warning(
                            "Proposal %s content shrank %.0f%% "
                            "(was %d, now %d bytes)",
                            prop["id"], shrinkage, stored_size, current_size,
                        )

            prompt = await self._build_dispatch_prompt(prop)
            action_type = prop.get("action_type", "")
            profile = _infer_profile(action_type)

            # Model selection: config override > profile-based default.
            # Investigations need Opus for deeper reasoning (verify APIs,
            # query event tables, question suspicious signals).
            model_override = self._config.dispatch_model_overrides.get(action_type)
            if model_override:
                model = CCModel(model_override)
            elif profile == "interact":
                model = CCModel.OPUS
            else:
                model = CCModel.SONNET

            # Atomically claim the proposal BEFORE spawning to prevent
            # double-dispatch (_process_execution_briefs is a second path).
            # Use raw SQL to preserve resolved_at (the approval timestamp
            # the staleness guard depends on).
            cursor = await self._db.execute(
                "UPDATE ego_proposals SET status = 'executed', "
                "user_response = 'dispatching' "
                "WHERE id = ? AND status = 'approved'",
                (prop["id"],),
            )
            await self._db.commit()
            if cursor.rowcount == 0:
                logger.debug(
                    "Proposal %s already claimed — skipping",
                    prop["id"],
                )
                continue

            try:
                request = DirectSessionRequest(
                    prompt=prompt,
                    profile=profile,
                    model=model,
                    effort=EffortLevel.HIGH,
                    notify=True,
                    source_tag="ego_dispatch",
                    caller_context=f"ego_proposal:{prop['id']}",
                )
                session_id = await self._direct_session_runner.spawn(request)
                # Update with actual session ID
                await self._db.execute(
                    "UPDATE ego_proposals SET user_response = ? WHERE id = ?",
                    (f"session:{session_id}", prop["id"]),
                )
                await self._db.commit()
                dispatched.append(prop["id"])
                logger.info(
                    "Sweep dispatched proposal %s → session %s",
                    prop["id"],
                    session_id,
                )
                # Send execution notification to ego_proposals topic
                await self._notify_execution(prop, session_id)
            except Exception:
                logger.error(
                    "Sweep failed to dispatch proposal %s — reverting to approved",
                    prop["id"],
                    exc_info=True,
                )
                try:
                    # Revert so sweep can retry on next cycle
                    await self._db.execute(
                        "UPDATE ego_proposals SET status = 'approved', "
                        "user_response = NULL WHERE id = ?",
                        (prop["id"],),
                    )
                    await self._db.commit()
                except Exception:
                    logger.error(
                        "Failed to revert proposal %s — stuck at executed",
                        prop["id"],
                        exc_info=True,
                    )

        if dispatched:
            logger.info("Sweep dispatched %d approved proposal(s)", len(dispatched))
        return dispatched

    async def _build_dispatch_prompt(self, prop: dict) -> str:
        """Build enriched dispatch prompt with world model context."""
        parts = [
            f"Execute this approved proposal:\n\n{prop['content']}",
            f"\nExecution plan: {prop.get('execution_plan') or 'N/A'}",
            f"\nRationale: {prop.get('rationale') or ''}",
        ]

        # Post-dispatch verification context
        eo_raw = prop.get("expected_outputs")
        if eo_raw:
            try:
                import json as _json

                parsed_eo = _json.loads(eo_raw) if isinstance(eo_raw, str) else eo_raw
                if isinstance(parsed_eo, dict) and parsed_eo.get("files"):
                    eo_lines = [
                        "\nExpected outputs (auto-verified after completion):",
                        f"  Files: {', '.join(parsed_eo['files'])}",
                    ]
                    if parsed_eo.get("min_size_bytes"):
                        eo_lines.append(f"  Min size: {parsed_eo['min_size_bytes']} bytes")
                    if parsed_eo.get("required_strings"):
                        eo_lines.append(
                            f"  Required content: {', '.join(parsed_eo['required_strings'])}"
                        )
                    parts.append("\n".join(eo_lines))
            except (ValueError, TypeError):
                pass

        # World model context — ONLY for non-content dispatches.
        # Content dispatches get minimal context to prevent information
        # leakage (goals, contacts, events are leak vectors for published
        # content). The proposal content + exec plan + rationale is sufficient.
        if not _is_content_dispatch(prop):
            try:
                from genesis.db.crud import user_goals

                goals = await user_goals.list_active(self._db)
                if goals:
                    goal_lines = [
                        f"- {g['title']} ({g['category']}, {g['priority']})" for g in goals[:5]
                    ]
                    parts.append("\n\nUser's active goals:\n" + "\n".join(goal_lines))
            except Exception:
                pass

            try:
                from genesis.db.crud import user_contacts

                contacts = await user_contacts.recently_active(self._db, days=14)
                if contacts:
                    contact_lines = [
                        f"- {c['name']} ({c.get('relationship', 'contact')})" for c in contacts[:5]
                    ]
                    parts.append("\nRelevant contacts:\n" + "\n".join(contact_lines))
            except Exception:
                pass

            try:
                from genesis.db.crud import memory_events

                events = await memory_events.upcoming_user_events(self._db, days=14)
                if events:
                    event_lines = [
                        f"- {e['object']} ({e.get('event_date', 'TBD')})" for e in events[:5]
                    ]
                    parts.append("\nUpcoming events:\n" + "\n".join(event_lines))
            except Exception:
                pass

        # Append content firewall rules to all dispatches as safety net
        parts.append(f"\n\n{_CONTENT_FIREWALL_RULES}")

        return "\n".join(parts)

    async def _notify_execution(self, prop: dict, session_id: str) -> None:
        """Send structured notification to ego_dispatches topic."""
        try:
            import html as html_mod

            tm = self._proposals._topic_manager
            if tm is None:
                return
            content = html_mod.escape(prop.get("content", "")[:200])
            action = html_mod.escape(prop.get("action_type", "unknown"))
            msg = f"<b>Dispatched</b> [{action}]: {content}\n<i>Session:</i> {session_id}"
            await tm.send_to_category("ego_dispatches", msg)
        except Exception:
            logger.debug("Failed to send execution notification", exc_info=True)


# -- User ego domain boundary -----------------------------------------------

# User ego blocked categories — infrastructure belongs to genesis ego.
# Uses prefix matching: "infrastructure" matches "infrastructure_bug", etc.
_USER_EGO_INFRA_PREFIXES = frozenset({
    "system_health", "infrastructure", "performance",
    "maintenance", "security", "cost_protection",
    "system_monitoring", "genesis_maintenance",
})


def _normalize_to_infra(category: str) -> bool:
    """Check if a category belongs to the infrastructure domain.

    Uses prefix matching to catch LLM-generated variants like
    'infrastructure_maintenance', 'infrastructure_bug', etc.
    """
    if not category:
        return False
    cat_lower = category.lower()
    return any(cat_lower.startswith(prefix) for prefix in _USER_EGO_INFRA_PREFIXES)


# -- Content dispatch detection + firewall rules ----------------------------

_CONTENT_DISPATCH_KEYWORDS = frozenset({
    "publish", "article", "medium", "post", "draft", "blog",
})
# "content" omitted — too broad for substring matching (matches
# "content_hash", "content error rate"). Caught by action_type check.


def _is_content_dispatch(prop: dict) -> bool:
    """Check if a proposal is a content/publishing dispatch.

    Used to apply information minimization at the dispatch boundary —
    content dispatches should NOT receive world model context (goals,
    contacts, events) that could leak into published output.
    """
    action_type = (prop.get("action_type") or "").lower()
    if action_type in ("outreach", "dispatch", "content", "publish"):
        return True
    content = (prop.get("content") or "").lower()
    return any(kw in content for kw in _CONTENT_DISPATCH_KEYWORDS)


_CONTENT_FIREWALL_RULES = """
## Content Firewall (externally-shared content only)
Principle: release no more information than the task requires.
- Use generic descriptions for private events ("a recent panel", not
  the event name)
- Use roles for people ("an engineer", not personal names)
- Never reference active job search, applications, or career workflows
- Never include calendar details, internal codenames, or infrastructure
- Biographical detail: only if directly relevant to the thesis
- When in doubt, omit. The user reviews before publish.
""".strip()


# -- Realist prompt & parser -----------------------------------------------

_NEUTRAL_STATUS = NEUTRAL_STATUS  # re-export for backwards compat


def _build_realist_prompt(
    proposals: list[dict],
    recent_history: list[dict],
    *,
    ego_source: str = "",
) -> str:
    """Build the realist evaluation prompt.

    Kept as small as possible — the realist prompt is ~2-3K input tokens
    vs the ego's 50-80K, so cost is modest even at Opus pricing.
    """
    history_lines = []
    if recent_history:
        history_lines.append("| Type | Topic | Outcome | When |")
        history_lines.append("|------|-------|---------|------|")
        for h in recent_history:
            action = h.get("action_type", "?")
            content = (h.get("content") or "")[:100].replace("\n", " ").replace("|", "/")
            status = _NEUTRAL_STATUS.get(h.get("status", ""), h.get("status", "?"))
            created = (h.get("created_at") or "")[:16]
            history_lines.append(f"| {action} | {content} | {status} | {created} |")
    else:
        history_lines.append("*No recent proposals.*")

    proposal_lines = []
    for i, p in enumerate(proposals):
        content = (p.get("content") or "")[:300].replace("\n", " ")
        action = p.get("action_type", "?")
        conf = p.get("confidence", 0.0)
        proposal_lines.append(f"{i}. [{action}] (confidence: {conf:.2f}) {content}")

    # Domain boundary context — ego-specific jurisdiction framing.
    ego_section = ""
    if ego_source == "genesis_ego_cycle":
        ego_section = """
## Ego Source
These proposals are from the **Genesis ego (COO/operations)**.
Its jurisdiction is Genesis infrastructure ONLY: system health,
performance, maintenance, and operational reliability. It has NO
jurisdiction over the user's career, content publishing, social media,
marketing, outreach strategy, job applications, networking, conferences,
personal scheduling, or external platforms Genesis doesn't operate.

"""
    elif ego_source == "user_ego_cycle":
        ego_section = """
## Ego Source
These proposals are from the **User ego (CEO)**.
Its jurisdiction is user value ONLY: career, content, goals,
networking, and personal advancement. It has NO jurisdiction over
Genesis infrastructure, system health, cost tracking, performance
monitoring, or internal maintenance.

"""

    domain_rule = ""
    if ego_source == "genesis_ego_cycle":
        domain_rule = """
7. **Domain boundary (operations ego only).** These proposals come from
   the operations ego. REJECT any proposal outside Genesis infrastructure.
   Career, job applications, content publishing, social media, marketing,
   outreach strategy, networking, conferences, personal scheduling, and
   external platforms are user ego domain. If a proposal mentions a
   user-domain topic as context for an infrastructure fix (e.g., "CDP
   dropped during a job application session"), AMEND to focus on the
   infrastructure component only and remove the user-domain framing.
"""
    elif ego_source == "user_ego_cycle":
        domain_rule = """
7. **Domain boundary (user ego only).** These proposals come from the
   user ego. REJECT any proposal about Genesis infrastructure, system
   health, cost optimization, performance tuning, or internal
   maintenance. Those belong to the Genesis ego (COO).
"""

    # Build Rule #1 based on ego source — genesis ego is allowed to
    # propose investigation dispatches (background sessions for diagnosis),
    # while user ego should do read-only work in-cycle.
    if ego_source == "genesis_ego_cycle":
        rule_1 = """
1. **Dispatch investigations are valid proposals.** The genesis ego may
   propose dispatching investigation sessions for issues that require
   dedicated time beyond the current cycle. Pure in-cycle reads (health
   checks, observation queries) should still be done in-cycle, but
   proposing a background session to diagnose a complex issue is a
   legitimate maintenance action. PASS these unless they are clearly
   something the ego could resolve with a single MCP tool call."""
    else:
        rule_1 = """
1. **Read operations are NOT proposals.** Investigating, researching, reading,
   profiling, querying, checking, monitoring — these are things the ego should
   do during its normal cycle without asking permission. If a proposal is
   purely investigative with no write/action/outreach component, REJECT it:
   "Read operation — do this during your cycle, don't propose it.\""""

    return f"""You are the Realist — a quality gate for ego proposals. Evaluate each
proposal and return a JSON array of verdicts.
{ego_section}
## Rules
{rule_1}

2. **Check for zombies.** If a proposal covers substantially the same topic
   as a recent proposal that was recycled/withdrawn/deferred/expired, and
   nothing has changed in the circumstances, REJECT: "Zombie — proposed
   before with no change in circumstances."

3. **Check feasibility.** If a proposal requires a capability Genesis
   genuinely doesn't have, AMEND with a feasible alternative.

4. **Check actionability.** If the proposal is too vague to execute,
   AMEND with concrete steps.

5. **Err on the side of passing.** When in doubt, PASS. The user is the
   final gate. Your job is to catch clear issues, not second-guess
   creative proposals.

6. **Do not confabulate system state.** You only know what is in the
   history table above. Do NOT make claims about whether infrastructure,
   pipelines, or capabilities are "broken" or "working" based on
   patterns in the history. A proposal that failed before may succeed
   now — circumstances change. Judge the proposal on its own merits,
   not on inferred system state.
{domain_rule}
## Recent Proposal History (48h)
{chr(10).join(history_lines)}

## New Proposals to Evaluate
{chr(10).join(proposal_lines)}

## Output Format
Return ONLY a JSON array, one entry per proposal (same order as input):
[{{"index": 0, "verdict": "pass|amend|reject", "reasoning": "brief explanation", "amended_content": "only if verdict is amend"}}]"""


def _parse_realist_response(
    raw_text: str,
    num_proposals: int,
) -> dict[int, dict]:
    """Parse the realist's JSON response into per-proposal verdicts.

    Returns {index: {"verdict": str, "reasoning": str, "amended_content": str?}}.
    On parse failure, returns empty dict (all proposals pass through).
    """
    if not raw_text or not raw_text.strip():
        return {}

    text = raw_text.strip()

    # Try direct parse
    parsed = None
    with contextlib.suppress(json.JSONDecodeError):
        parsed = json.loads(text)

    # Try markdown code block
    if parsed is None:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if match:
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(match.group(1).strip())

    # Try bracket extraction
    if parsed is None:
        first = text.find("[")
        last = text.rfind("]")
        if first != -1 and last > first:
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(text[first : last + 1])

    if not isinstance(parsed, list):
        logger.warning("Realist response is not a JSON array: %.200s", text)
        return {}

    verdicts: dict[int, dict] = {}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("index", -1))
        except (ValueError, TypeError):
            continue
        if idx < 0 or idx >= num_proposals:
            continue

        verdict = entry.get("verdict", "pass")
        if verdict not in ("pass", "amend", "reject"):
            verdict = "pass"

        verdicts[idx] = {
            "verdict": verdict,
            "reasoning": str(entry.get("reasoning", ""))[:500],
        }
        if verdict == "amend" and entry.get("amended_content"):
            verdicts[idx]["amended_content"] = str(entry["amended_content"])[:2000]

    return verdicts


_VALID_URGENCIES = frozenset({"low", "normal", "high", "critical"})

# -- Behavioral focus detection --------------------------------------------
#
# focus_summary must describe a TOPIC the ego is thinking about, never a
# BEHAVIORAL state.  This is a broad structural safety net — it catches
# any focus starting with a self-referential behavioral verb (holding,
# Focus sanitization removed — focus_summary is now system-computed
# from DB state (computed_focus.py), not ego-authored. The ego cannot
# encode behavioral states into focus because it doesn't write it.
#
# essential_knowledge.py retains its own copy of _BEHAVIORAL_FOCUS_RE
# as defense-in-depth for the essential knowledge injection path.


_INTERACT_TYPES = frozenset({"outreach", "dispatch", "publish"})
_RESEARCH_TYPES = frozenset({"investigate"})


def _infer_profile(action_type: str) -> str:
    """Map proposal action_type to a DirectSession profile."""
    if action_type in _INTERACT_TYPES:
        return "interact"
    if action_type in _RESEARCH_TYPES:
        return "research"
    return "observe"


def _validate_output(data: dict) -> dict | None:
    """Minimal validation of ego output structure.

    Checks required fields exist with correct types.
    Sanitizes individual proposal fields to prevent DB constraint violations.
    Returns data if valid, None if not.
    """
    if not isinstance(data.get("proposals"), list):
        logger.warning("Ego output missing or invalid 'proposals' field")
        return None
    if not isinstance(data.get("focus_summary"), str):
        logger.warning("Ego output missing or invalid 'focus_summary' field")
        return None
    # follow_ups is no longer required in the output contract.
    # Accept presence or absence gracefully.
    if "follow_ups" in data and not isinstance(data["follow_ups"], list):
        data["follow_ups"] = []

    # Focus sanitization removed — focus_summary is system-computed
    # (computed_focus.py). The ego's authored focus is logged in
    # ego_cycles for audit but not persisted to ego_state.

    # Sanitize individual proposals to prevent DB constraint violations.
    for p in data["proposals"]:
        if not isinstance(p, dict):
            continue
        if p.get("urgency") not in _VALID_URGENCIES:
            p["urgency"] = "normal"
        try:
            p["confidence"] = float(p.get("confidence", 0.0))
        except (ValueError, TypeError):
            p["confidence"] = 0.0

    # Sanitize knowledge_updates — legacy field, log if ego still outputs it.
    if "knowledge_updates" in data:
        raw = data["knowledge_updates"]
        if isinstance(raw, list) and raw:
            logger.info(
                "Ego output contains %d knowledge_updates (notepad removed, ignored)",
                len(raw),
            )
        del data["knowledge_updates"]

    # Sanitize intentions — validate structure.
    if "intentions" in data:
        raw = data["intentions"]
        if not isinstance(raw, dict):
            data["intentions"] = {"review": [], "new": []}
        else:
            # Sanitize review entries
            review = raw.get("review", [])
            if not isinstance(review, list):
                review = []
            raw["review"] = [
                r for r in review
                if isinstance(r, dict)
                and isinstance(r.get("id"), str)
                and r.get("action") in ("keep", "fire", "withdraw", "renew")
            ]
            # Sanitize new entries
            new = raw.get("new", [])
            if not isinstance(new, list):
                new = []
            raw["new"] = [
                n for n in new
                if isinstance(n, dict)
                and isinstance(n.get("content"), str)
                and isinstance(n.get("trigger_condition"), str)
            ]
            data["intentions"] = raw

    # Sanitize resolved_directives — filter malformed entries.
    if "resolved_directives" in data:
        raw = data["resolved_directives"]
        if not isinstance(raw, list):
            data["resolved_directives"] = []
        else:
            data["resolved_directives"] = [
                r
                for r in raw
                if isinstance(r, dict)
                and isinstance(r.get("id"), str)
                and r["id"]
            ]

    # Sanitize goal_assessment — optional free text, string only.
    if "goal_assessment" in data and not isinstance(
        data["goal_assessment"], str
    ):
        data["goal_assessment"] = ""

    # Sanitize goal_status_recommendation — optional enum.
    _VALID_GOAL_RECS = ("continue", "pause", "deprioritize", "close")
    if (
        "goal_status_recommendation" in data
        and data["goal_status_recommendation"] not in _VALID_GOAL_RECS
    ):
        del data["goal_status_recommendation"]

    return data
