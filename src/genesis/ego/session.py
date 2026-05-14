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
    CYCLE_TYPE_DEFAULTS,
    NEUTRAL_STATUS,
    CycleType,
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
    from genesis.ego.proposals import ProposalWorkflow
    from genesis.observability.events import GenesisEventBus

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "identity" / "EGO_SESSION.md"
_DEFAULT_CALL_SITE = "7_ego_cycle"
_DEFAULT_FOCUS_SUMMARY_KEY = "ego_focus_summary"


class BudgetExceededError(Exception):
    """Raised when the ego's daily budget cap is exceeded."""


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
        self._mcp_config_path = mcp_config_path
        self._call_site = call_site or _DEFAULT_CALL_SITE
        self._focus_summary_key = focus_summary_key or _DEFAULT_FOCUS_SUMMARY_KEY
        self._source_tag = source_tag or "ego_cycle"
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

    def set_autonomous_dispatcher(self, dispatcher: object) -> None:
        self._autonomous_dispatcher = dispatcher

    # -- Public API --------------------------------------------------------

    async def run_cycle(
        self,
        *,
        is_morning_report: bool = False,
        cycle_type: CycleType | None = None,
        model_override: str | None = None,
    ) -> EgoCycle | None:
        """Execute one ego thinking cycle.

        Parameters
        ----------
        is_morning_report:
            Legacy flag, still supported. Equivalent to
            ``cycle_type=CycleType.MORNING_REPORT``.
        cycle_type:
            Determines model and effort for this cycle. If None,
            inferred from ``is_morning_report``.
        model_override:
            Override model selection (e.g., "opus" for deep-think cycles).
            Takes precedence over both config and cycle_type defaults.

        Returns the stored EgoCycle, or None if the cycle failed (CC error).

        Raises:
            BudgetExceededError: Daily budget cap exceeded (not a failure).
            CycleBlockedError: Approval gate blocked the cycle (not a failure).
        """
        # Resolve cycle type
        if cycle_type is None:
            cycle_type = CycleType.MORNING_REPORT if is_morning_report else CycleType.PROACTIVE
        is_morning_report = cycle_type == CycleType.MORNING_REPORT

        # Select model + effort: config is the base, cycle type overrides
        # only for specific types (morning report → sonnet/low, etc.)
        cycle_model, cycle_effort = CYCLE_TYPE_DEFAULTS.get(cycle_type, (None, None))
        model = CCModel(cycle_model or self._config.model)
        effort = EffortLevel(cycle_effort or self._config.default_effort)

        # model_override takes precedence (e.g., deep-think Opus cycle)
        if model_override:
            model = CCModel(model_override)

        # 1. Budget check — raises BudgetExceededError (not a failure)
        if not await self._check_budget():
            raise BudgetExceededError(
                f"Daily ego thinking spend exceeds cap ${self._config.ego_thinking_budget_usd}"
            )

        # 2. Assemble operational context (previous focus + fresh context)
        dynamic_context = await self._compaction.assemble_context(
            context_builder=self._context_builder,
        )

        # 3. Build prompts — system prompt is identity ONLY (cacheable),
        #    operational context goes in the user message.
        system_prompt = self._static_prompt
        user_prompt = self._build_user_prompt(
            dynamic_context=dynamic_context,
            is_morning_report=is_morning_report,
        )

        # 4. Build invocation — ephemeral (no resume).
        # append_system_prompt=True: preserve CC's tool framework
        # underneath the ego identity prompt (see feedback_append_system_prompt.md).
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
                logger.warning("Ego cycle blocked: %s", decision.reason)
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
                logger.error("Failed to create ego background session", exc_info=True)
                return None

            try:
                output = await self._invoker.run(invocation)
            except Exception:
                logger.error("Ego CC invocation failed", exc_info=True)
                try:
                    await self._session_manager.fail(session_id, reason="CC invocation error")
                except Exception:
                    logger.error("Session fail() also errored", exc_info=True)
                return None

        if output.is_error:
            logger.error("Ego CC session returned error: %s", output.error_message)
            if session_id is not None:
                try:
                    await self._session_manager.fail(session_id, reason=output.error_message)
                except Exception:
                    logger.error("Session fail() also errored", exc_info=True)
            return None

        # 6. Parse output
        parsed = self._parse_output(output.text)

        # 6b. If focus was sanitized, try previous legitimate focus as fallback
        if parsed and parsed.get("_focus_violation"):
            prev = await ego_crud.get_state(
                self._db,
                self._focus_summary_key,
            )
            if prev and not _BEHAVIORAL_FOCUS_RE.search(prev):
                parsed["focus_summary"] = prev

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

        # 9. Process proposals
        if parsed:
            proposals = parsed.get("proposals", [])
            comm_decision = parsed.get("communication_decision", "send_digest")
            if proposals:
                # Realist gate — LLM evaluates proposals against history
                proposals = await self._filter_proposals(proposals)
                # Log realist cost for observability (cycle dataclass is frozen,
                # so cost is tracked via logging; negligible vs ego cycle cost)
                if self._last_realist_cost_usd > 0:
                    logger.info(
                        "Realist cost: $%.4f (ego cycle: $%.4f)",
                        self._last_realist_cost_usd,
                        cycle.cost_usd,
                    )
                if proposals:
                    await self._process_proposals(
                        proposals,
                        cycle.id,
                        communication_decision=comm_decision,
                    )

            # 9b. Process tabled/withdrawn proposal IDs
            tabled_ids = parsed.get("tabled", [])
            if isinstance(tabled_ids, list):
                for pid in tabled_ids:
                    if isinstance(pid, str) and pid:
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

            # 10c. Process knowledge notepad updates (user ego only)
            knowledge_updates = parsed.get("knowledge_updates", [])
            if knowledge_updates and self._source_tag == "user_ego_cycle":
                await self._apply_knowledge_updates(knowledge_updates)

            # 11. Store focus summary for reflection injection
            if focus:
                await ego_crud.set_state(
                    self._db,
                    key=self._focus_summary_key,
                    value=focus,
                )

            # 11b. Record violation if focus was behavioral self-assignment
            if parsed.get("_focus_violation"):
                import uuid

                from genesis.db.crud import observations as obs_crud

                try:
                    await obs_crud.create(
                        self._db,
                        id=str(uuid.uuid4()),
                        source="ego_session",
                        type="ego_focus_violation",
                        content=(
                            f"Ego attempted behavioral self-assignment in "
                            f"focus_summary. Original: "
                            f"{parsed.get('_original_focus', 'unknown')[:200]}. "
                            f"Sanitized to: {focus}"
                        ),
                        priority="medium",
                        created_at=datetime.now(UTC).isoformat(),
                        category="system_health",
                    )
                except Exception:
                    logger.warning(
                        "Failed to record focus violation observation",
                        exc_info=True,
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

    def _build_user_prompt(
        self,
        *,
        dynamic_context: str,
        is_morning_report: bool,
    ) -> str:
        """Build the user message: operational context + directive.

        The system prompt is the static identity only (cacheable).
        All dynamic content goes here in the user message.
        """
        directive = (
            "Run your ego cycle. Review the operational context below, "
            "check your open threads, and use your MCP tools to verify "
            "any beliefs before proposing actions. End with valid JSON "
            "matching the ego output schema."
        )
        if is_morning_report:
            directive += (
                "\n\nThis is a MORNING REPORT cycle. Include the morning_report "
                "field with your daily briefing for the user."
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

        prompt = _build_realist_prompt(proposals, recent)

        try:
            invocation = CCInvocation(
                prompt=prompt,
                model=CCModel.SONNET,
                effort=EffortLevel.LOW,
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

            if rejected_count or amended_count:
                logger.info(
                    "Realist: %d/%d passed (%d rejected, %d amended)",
                    len(filtered),
                    len(proposals),
                    rejected_count,
                    amended_count,
                )

            return filtered
        except Exception:
            logger.warning("Realist filter failed, passing through", exc_info=True)
            return proposals

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

        # Check dispatch budget before spawning any sessions
        if not await self._check_dispatch_budget():
            logger.warning("Dispatch budget exceeded — skipping execution briefs")
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

            # Verify the proposal is actually approved
            proposal = await ego_crud.get_proposal(self._db, proposal_id)
            if not proposal or proposal["status"] != "approved":
                logger.warning(
                    "Execution brief for proposal %s skipped (status=%s)",
                    proposal_id,
                    proposal["status"] if proposal else "not found",
                )
                continue

            # Map profile and model from brief
            profile = brief.get("profile", "observe")
            if profile not in VALID_PROFILES:
                profile = "observe"
            model_str = brief.get("model", "sonnet")
            model = CCModel.SONNET if model_str != "haiku" else CCModel.HAIKU

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
                await ego_crud.execute_proposal(
                    self._db,
                    proposal_id,
                    status="executed",
                    user_response=f"session:{session_id}",
                )
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
                    "Failed to dispatch proposal %s",
                    proposal_id,
                    exc_info=True,
                )
                try:
                    await ego_crud.execute_proposal(
                        self._db,
                        proposal_id,
                        status="failed",
                        user_response="dispatch failed",
                    )
                except Exception:
                    logger.error(
                        "Failed to mark proposal %s as failed",
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
        """
        import uuid

        from genesis.db.crud import observations as obs_crud

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

            try:
                await obs_crud.create(
                    self._db,
                    id=str(uuid.uuid4()),
                    source="genesis_ego",
                    type="escalation_to_user_ego",
                    content="\n".join(content_parts),
                    priority="high",
                    created_at=datetime.now(UTC).isoformat(),
                    category="escalation",
                )
            except Exception:
                logger.error(
                    "Failed to write escalation from cycle %s",
                    cycle_id,
                    exc_info=True,
                )

        if escalations:
            logger.info(
                "Genesis ego cycle %s produced %d escalation(s)",
                cycle_id,
                len(escalations),
            )

    # -- Knowledge notepad --------------------------------------------------

    _NOTEPAD_PATH = Path(__file__).resolve().parent.parent / "identity" / "EGO_NOTEPAD.md"
    _NOTEPAD_MARKER = "# Ego Notepad"

    async def _apply_knowledge_updates(
        self,
        updates: list[dict],
    ) -> None:
        """Apply incremental updates to EGO_NOTEPAD.md.

        Each update: {section, action (add|update|remove), content, replaces?}
        """
        try:
            if self._NOTEPAD_PATH.exists():
                text = self._NOTEPAD_PATH.read_text()
            else:
                # Seed from example template
                example = self._NOTEPAD_PATH.with_suffix(".md.example")
                text = example.read_text() if example.exists() else ""

            if not text.strip():
                logger.warning("Ego notepad is empty — skipping updates")
                return

            sections = _parse_notepad_sections(text)
            today = datetime.now(UTC).strftime("%Y-%m-%d")

            applied = 0
            for u in updates:
                section_name = u["section"]
                action = u["action"]
                # Sanitize: strip newlines (break markdown list), cap length
                content = u["content"].replace("\n", " ").strip()[:500]

                if section_name not in sections:
                    logger.warning(
                        "Ego notepad: unknown section %r — skipping",
                        section_name,
                    )
                    continue

                entries = sections[section_name]["entries"]
                cap = sections[section_name]["cap"]

                if action == "add":
                    entries.append(f"- [{today}] {content}")
                    # Enforce cap — trim oldest
                    if cap and len(entries) > cap:
                        entries[:] = entries[-cap:]
                    applied += 1

                elif action == "update":
                    replaces = u.get("replaces", "")
                    if not replaces:
                        continue
                    for i, entry in enumerate(entries):
                        if replaces in entry:
                            entries[i] = f"- [{today}] {content}"
                            applied += 1
                            break

                elif action == "remove":
                    for i, entry in enumerate(entries):
                        if content in entry:
                            entries.pop(i)
                            applied += 1
                            break

            if applied == 0:
                return

            # Rebuild file
            result = _rebuild_notepad(sections, today)
            self._NOTEPAD_PATH.write_text(result)
            logger.info(
                "Ego notepad: applied %d/%d updates",
                applied,
                len(updates),
            )
        except Exception:
            logger.error("Failed to apply ego notepad updates", exc_info=True)

    # -- Approved proposal sweep --------------------------------------------

    async def sweep_approved_proposals(self) -> list[str]:
        """Mechanically dispatch approved proposals via DirectSessionRunner.

        Called on a fixed 30-minute interval by EgoCadenceManager AND
        immediately after user approval via Telegram.  The sweep lock
        prevents concurrent execution (double-dispatch guard).

        Returns list of dispatched proposal IDs.
        """
        async with self._sweep_lock:
            return await self._sweep_approved_inner()

    async def _sweep_approved_inner(self) -> list[str]:
        """Inner sweep logic — must be called under self._sweep_lock."""
        if self._direct_session_runner is None:
            return []

        if not await self._check_dispatch_budget():
            logger.info("Sweep skipped — dispatch budget exceeded")
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
            # Staleness guard — skip proposals approved more than 48h ago.
            # resolved_at is set by resolve_proposal() at approval time.
            try:
                approved_at = datetime.fromisoformat(prop["resolved_at"])
                if datetime.now(UTC) - approved_at > timedelta(hours=48):
                    continue
            except (KeyError, TypeError, ValueError):
                continue

            prompt = await self._build_dispatch_prompt(prop)
            profile = _infer_profile(prop.get("action_type", ""))

            try:
                request = DirectSessionRequest(
                    prompt=prompt,
                    profile=profile,
                    model=CCModel.SONNET,
                    effort=EffortLevel.HIGH,
                    notify=True,
                    source_tag="ego_dispatch",
                    caller_context=f"ego_proposal:{prop['id']}",
                )
                session_id = await self._direct_session_runner.spawn(request)
                ok = await ego_crud.execute_proposal(
                    self._db,
                    prop["id"],
                    status="executed",
                    user_response=f"session:{session_id}",
                )
                if ok:
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
                    "Sweep failed to dispatch proposal %s",
                    prop["id"],
                    exc_info=True,
                )
                try:
                    await ego_crud.execute_proposal(
                        self._db,
                        prop["id"],
                        status="failed",
                        user_response="sweep_dispatch_error",
                    )
                except Exception:
                    logger.error(
                        "Failed to mark proposal %s as failed",
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

        # World model context — each section degrades independently
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

        return "\n".join(parts)

    async def _notify_execution(self, prop: dict, session_id: str) -> None:
        """Send structured notification to ego_proposals topic."""
        try:
            import html as html_mod

            tm = self._proposals._topic_manager
            if tm is None:
                return
            content = html_mod.escape(prop.get("content", "")[:200])
            action = html_mod.escape(prop.get("action_type", "unknown"))
            msg = f"<b>Dispatched</b> [{action}]: {content}\n<i>Session:</i> {session_id}"
            await tm.send_to_category("ego_proposals", msg)
        except Exception:
            logger.debug("Failed to send execution notification", exc_info=True)

    async def _check_budget(self) -> bool:
        """True if daily ego thinking spend is under the budget cap."""
        try:
            daily = await ego_crud.daily_ego_cost(self._db)
            if daily >= self._config.ego_thinking_budget_usd:
                logger.warning(
                    "Ego thinking spend $%.2f exceeds cap $%.2f",
                    daily,
                    self._config.ego_thinking_budget_usd,
                )
                return False
        except Exception:
            logger.warning("Budget check failed — allowing cycle", exc_info=True)
        return True

    async def _check_dispatch_budget(self) -> bool:
        """True if daily ego dispatch spend is under the budget cap."""
        try:
            daily = await ego_crud.daily_dispatch_cost(self._db)
            if daily >= self._config.ego_dispatch_budget_usd:
                logger.warning(
                    "Ego dispatch spend $%.2f exceeds cap $%.2f",
                    daily,
                    self._config.ego_dispatch_budget_usd,
                )
                return False
        except Exception:
            logger.warning("Dispatch budget check failed — allowing", exc_info=True)
        return True


# -- Realist prompt & parser -----------------------------------------------

_NEUTRAL_STATUS = NEUTRAL_STATUS  # re-export for backwards compat


def _build_realist_prompt(
    proposals: list[dict],
    recent_history: list[dict],
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

    return f"""You are the Realist — a quality gate for ego proposals. Evaluate each
proposal and return a JSON array of verdicts.

## Rules

1. **Read operations are NOT proposals.** Investigating, researching, reading,
   profiling, querying, checking, monitoring — these are things the ego should
   do during its normal cycle without asking permission. If a proposal is
   purely investigative with no write/action/outreach component, REJECT it:
   "Read operation — do this during your cycle, don't propose it."

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
_VALID_NOTEPAD_ACTIONS = frozenset({"add", "update", "remove"})

# -- Behavioral focus detection --------------------------------------------
#
# focus_summary must describe a TOPIC the ego is thinking about, never a
# BEHAVIORAL state.  This is a broad structural safety net — it catches
# any focus starting with a self-referential behavioral verb (holding,
# waiting, stepping, etc.) regardless of what follows.  The primary fix
# is removing the signals that trigger self-suppression (user activity
# metrics, proposal engagement data, communication_decision lever).
# This regex is the last line of defense.
#
# Keep in sync with essential_knowledge._BEHAVIORAL_FOCUS_RE.
_BEHAVIORAL_FOCUS_RE = re.compile(
    r"(?i)"
    # Any focus starting with a self-referential behavioral verb.
    # These describe what the ego IS DOING, not a topic.
    r"(?:^(?:holding|waiting|stepping|standing|lying|staying|backing|"
    r"keeping|pausing|going|hibernating|letting|until|not)\s"
    # Explicit non-action / dormancy phrasing
    r"|^observing\s+(?:only|quietly)"
    r"|^passive\s+(?:mode|watch)"
    r"|^minimal\s+(?:engagement|activity)"
    r"|^reduced\s+(?:activity|engagement)"
    r"|quiet\s+mode"
    # Dormancy keywords anywhere
    r"|(?:self-|going\s+|entering\s+)dormant"
    r"|(?:going|entering)\s+fallow"
    # Proposal suppression
    r"|no\s+proposals?\s+(?:until|for\s+now))"
)

# Catches engagement-conditional language anywhere in focus text, e.g.
# "CC upgrade proposal held pending for when engagement resumes."
# This is separate from _BEHAVIORAL_FOCUS_RE because it matches mid-text
# clauses, not just the beginning of the focus string.  Matches from
# the trigger word through end of string (engagement clauses are tails).
_ENGAGEMENT_SUPPRESSION_RE = re.compile(
    r"(?i)\s*\b(?:held|deferred|delayed|postponed|tabled|shelved)\s+"
    r"(?:pending|until|for\s+when)\b.*$"
)


def _sanitize_focus_summary(
    focus: str,
    *,
    previous_focus: str | None = None,
) -> tuple[str, bool]:
    """Validate focus_summary describes a TOPIC, not a BEHAVIOR.

    Returns (sanitized_focus, was_violated).
    If the focus is behavioral, returns the previous legitimate focus
    or a generic fallback, plus was_violated=True.  If engagement-
    suppression language appears mid-text, that clause is stripped
    rather than replacing the entire focus.
    """
    if _BEHAVIORAL_FOCUS_RE.search(focus):
        fallback = previous_focus or "general system awareness"
        logger.warning(
            "Ego focus_summary contains behavioral self-assignment: %r — replacing with %r",
            focus[:120],
            fallback,
        )
        return fallback, True

    # Strip engagement-conditional clauses from mid-text without
    # replacing the entire focus (the topic portion may be valid).
    cleaned = _ENGAGEMENT_SUPPRESSION_RE.sub("", focus).strip().rstrip(";,")
    if cleaned != focus:
        logger.warning(
            "Ego focus_summary contains engagement-suppression clause: %r — stripped to %r",
            focus[:120],
            cleaned[:120],
        )
        return cleaned or (previous_focus or "general system awareness"), True

    return focus, False


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
    if not isinstance(data.get("follow_ups"), list):
        logger.warning("Ego output missing or invalid 'follow_ups' field")
        return None

    # Sanitize focus_summary — must describe a TOPIC, not a BEHAVIOR.
    sanitized, violated = _sanitize_focus_summary(data["focus_summary"])
    if violated:
        data["_original_focus"] = data["focus_summary"]
        data["focus_summary"] = sanitized
        data["_focus_violation"] = True

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

    # Sanitize knowledge_updates — filter malformed entries.
    if "knowledge_updates" in data:
        raw = data["knowledge_updates"]
        if not isinstance(raw, list):
            data["knowledge_updates"] = []
        else:
            data["knowledge_updates"] = [
                u
                for u in raw
                if isinstance(u, dict)
                and isinstance(u.get("section"), str)
                and u.get("action") in _VALID_NOTEPAD_ACTIONS
                and isinstance(u.get("content"), str)
            ]

    return data


# -- Notepad parsing helpers -----------------------------------------------

_CAP_PATTERN = re.compile(r"_\(max (\d+) items?\)_")

# Ordered sections for the ego notepad — defines output order.
_NOTEPAD_SECTIONS = [
    "Active Projects & Priorities",
    "Interests & Expertise",
    "Proposal Context Journal",
    "Open Questions",
]


def _parse_notepad_sections(text: str) -> dict[str, dict]:
    """Parse EGO_NOTEPAD.md into sections.

    Returns {section_name: {"cap": int|None, "entries": [str]}}
    preserving the header block (everything before the first ## section).
    """
    sections: dict[str, dict] = {}
    current_section: str | None = None
    header_lines: list[str] = []

    for line in text.splitlines():
        if line.startswith("## "):
            current_section = line[3:].strip()
            sections[current_section] = {"cap": None, "entries": []}
        elif current_section is None:
            header_lines.append(line)
        elif current_section in sections:
            cap_match = _CAP_PATTERN.search(line)
            if cap_match:
                sections[current_section]["cap"] = int(cap_match.group(1))
            elif line.startswith("- "):
                sections[current_section]["entries"].append(line)
            # Skip empty lines and other non-entry content

    # Store header for rebuild
    sections["__header__"] = {"cap": None, "entries": header_lines}
    return sections


def _rebuild_notepad(sections: dict[str, dict], today: str) -> str:
    """Rebuild EGO_NOTEPAD.md from parsed sections."""
    lines: list[str] = []

    # Header — update timestamp
    for line in sections.get("__header__", {}).get("entries", []):
        if "Last updated:" in line:
            lines.append(f"> Last updated: {today}")
        else:
            lines.append(line)
    lines.append("")

    # Sections in defined order, then any extras
    seen = {"__header__"}
    for name in _NOTEPAD_SECTIONS:
        if name in sections:
            _emit_section(lines, name, sections[name])
            seen.add(name)
    for name, data in sections.items():
        if name not in seen:
            _emit_section(lines, name, data)

    return "\n".join(lines) + "\n"


def _emit_section(lines: list[str], name: str, data: dict) -> None:
    """Emit a single section into the output lines."""
    lines.append(f"## {name}")
    cap = data.get("cap")
    if cap:
        lines.append(f"_(max {cap} items)_")
    entries = data.get("entries", [])
    if entries:
        lines.append("")
        lines.extend(entries)
    lines.append("")
