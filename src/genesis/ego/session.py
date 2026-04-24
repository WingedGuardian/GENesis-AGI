"""Ego session — persistent CC session with tool access.

Orchestrates: context assembly → CC invocation (with --resume for
continuity) → output parsing → cycle storage → proposal creation →
follow-up recording.

The ego maintains a persistent CC session via --resume.  Each cycle
resumes the prior session (conversational continuity) and injects
fresh operational context via --append-system-prompt.  The ego has
MCP tool access (genesis-health + genesis-memory) to verify beliefs
before proposing actions.
"""

from __future__ import annotations

import json
import logging
import re
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
from genesis.ego.types import EgoConfig, EgoCycle

if TYPE_CHECKING:
    import aiosqlite

    from genesis.cc.protocol import AgentProvider
    from genesis.cc.session_manager import SessionManager
    from genesis.ego.compaction import CompactionEngine
    from genesis.ego.context import EgoContextBuilder
    from genesis.ego.dispatch import EgoDispatcher
    from genesis.ego.proposals import ProposalWorkflow
    from genesis.observability.events import GenesisEventBus

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "identity" / "EGO_SESSION.md"
_CALL_SITE = "7_ego_cycle"


class BudgetExceededError(Exception):
    """Raised when the ego's daily budget cap is exceeded."""


class EgoSession:
    """Persistent CC session for ego thinking cycles.

    One instance is created at runtime startup and reused across cycles.
    The ego maintains a CC session ID across cycles via ``--resume`` for
    conversational continuity, and receives fresh operational context
    each cycle via ``--append-system-prompt``.
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
        mcp_config_path: str | None = None,
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
        self._autonomous_dispatcher = None
        self._mcp_config_path = mcp_config_path
        # Cache the static system prompt (read once, not every cycle)
        if _PROMPT_PATH.exists():
            self._static_prompt = _PROMPT_PATH.read_text()
        else:
            logger.warning("EGO_SESSION.md not found at %s", _PROMPT_PATH)
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
    ) -> EgoCycle | None:
        """Execute one ego thinking cycle.

        Returns the stored EgoCycle, or None if the cycle was skipped
        (budget exceeded) or failed (CC error).
        """
        # 1. Budget check — raises BudgetExceededError (not a failure)
        if not await self._check_budget():
            raise BudgetExceededError(
                f"Daily ego spend exceeds cap ${self._config.daily_budget_cap_usd}"
            )

        # 2. Compact old cycles (best-effort, failure is non-fatal)
        try:
            await self._compaction.maybe_compact()
        except Exception:
            logger.error("Compaction failed before ego cycle", exc_info=True)

        # 3. Assemble context
        dynamic_context = await self._compaction.assemble_context(
            context_builder=self._context_builder,
        )

        # 4. Build prompts
        system_prompt = self._build_system_prompt(dynamic_context)
        user_prompt = self._build_user_prompt(is_morning_report=is_morning_report)

        # 5. Determine effort level from config
        effort = EffortLevel(
            self._config.morning_report_effort if is_morning_report
            else self._config.default_effort
        )

        # 6. Check for stored CC session ID (persistent session)
        stored_cc_sid = await ego_crud.get_state(self._db, "cc_session_id")

        # 7. Build invocation — resume if we have a prior session
        if stored_cc_sid:
            invocation = CCInvocation(
                prompt=user_prompt,
                model=CCModel(self._config.model),
                effort=effort,
                resume_session_id=stored_cc_sid,
                append_system_prompt=True,
                system_prompt=system_prompt,
                skip_permissions=True,
                working_dir=background_session_dir(),
                mcp_config=self._mcp_config_path,
            )
        else:
            invocation = CCInvocation(
                prompt=user_prompt,
                model=CCModel(self._config.model),
                effort=effort,
                append_system_prompt=True,
                system_prompt=system_prompt,
                skip_permissions=True,
                working_dir=background_session_dir(),
                mcp_config=self._mcp_config_path,
            )

        output = None
        session_id: str | None = None
        if self._autonomous_dispatcher is not None:
            # Call-site gating pre-check: if an ego_cycle approval is
            # already pending, skip this cycle entirely.  No new approval
            # request is created (no Telegram spam), and the next scheduler
            # tick will re-check once the pending approval resolves.
            try:
                pending = await (
                    self._autonomous_dispatcher.approval_gate.find_site_pending(
                        subsystem="ego", policy_id="ego_cycle",
                    )
                )
            except Exception:
                logger.warning(
                    "find_site_pending failed for ego_cycle; proceeding without pre-check",
                    exc_info=True,
                )
                pending = None
            if pending is not None:
                logger.info(
                    "Ego cycle skipped — call site blocked on approval %s",
                    pending.get("id"),
                )
                return None

            decision = await self._autonomous_dispatcher.route(
                AutonomousDispatchRequest(
                    subsystem="ego",
                    policy_id="ego_cycle",
                    action_label="ego cycle",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    cli_invocation=invocation,
                    dispatch_mode="cli",
                    cli_fallback_allowed=True,
                    approval_required_for_cli=True,
                ),
            )
            if decision.mode == "blocked":
                logger.warning("Ego cycle blocked: %s", decision.reason)
                return None

        if output is None:
            try:
                sess = await self._session_manager.create_background(
                    session_type=SessionType.BACKGROUND_TASK,
                    model=CCModel(self._config.model),
                    effort=effort,
                    source_tag="ego_cycle",
                )
                session_id = sess["id"]
            except Exception:
                logger.error("Failed to create ego background session", exc_info=True)
                return None

            try:
                output = await self._invoker.run(invocation)
            except Exception:
                logger.error("Ego CC invocation failed", exc_info=True)
                # Resume failure — clear stored session ID so next cycle
                # starts fresh instead of retrying the broken session.
                if stored_cc_sid:
                    await ego_crud.set_state(self._db, key="cc_session_id", value="")
                    logger.info("Cleared stored cc_session_id after invocation failure")
                try:
                    await self._session_manager.fail(session_id, reason="CC invocation error")
                except Exception:
                    logger.error("Session fail() also errored", exc_info=True)
                return None

        if output.is_error:
            logger.error("Ego CC session returned error: %s", output.error_message)
            # Clear stored session ID on error — next cycle starts fresh
            if stored_cc_sid:
                await ego_crud.set_state(self._db, key="cc_session_id", value="")
                logger.info("Cleared stored cc_session_id after CC error")
            if session_id is not None:
                try:
                    await self._session_manager.fail(session_id, reason=output.error_message)
                except Exception:
                    logger.error("Session fail() also errored", exc_info=True)
            return None

        # 8. Store CC session ID for next cycle's --resume
        if output.session_id:
            await ego_crud.set_state(
                self._db, key="cc_session_id", value=output.session_id,
            )

        # 9. Parse output
        parsed = self._parse_output(output.text)

        # 10. Store cycle
        focus = parsed.get("focus_summary", "") if parsed else ""
        proposals_json = json.dumps(parsed.get("proposals", [])) if parsed else "[]"
        cycle = EgoCycle(
            output_text=output.text,
            proposals_json=proposals_json,
            focus_summary=focus,
            model_used=output.model_used or self._config.model,
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

            # 11. Record last run for neural monitor
            try:
                from genesis.observability.call_site_recorder import record_last_run
                await record_last_run(
                    self._db, _CALL_SITE,
                    provider="cc", model_id=output.model_used or self._config.model,
                    response_text=output.text[:500] if output.text else "",
                    input_tokens=output.input_tokens,
                    output_tokens=output.output_tokens,
                )
            except Exception:
                logger.warning("Failed to record ego last_run", exc_info=True)

        # 11. Process proposals
        if parsed:
            proposals = parsed.get("proposals", [])
            if proposals:
                await self._process_proposals(proposals, cycle.id)

            # 12. Record follow_ups
            follow_ups = parsed.get("follow_ups", [])
            if follow_ups:
                await self._dispatcher.record_follow_ups(follow_ups, cycle.id)

            # 13. Store focus summary for reflection injection
            if focus:
                await ego_crud.set_state(
                    self._db, key="ego_focus_summary", value=focus,
                )
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

    def _build_system_prompt(self, dynamic_context: str) -> str:
        """Combine cached static EGO_SESSION.md with dynamic operational context."""
        return f"{self._static_prompt}\n\n---\n\n{dynamic_context}"

    def _build_user_prompt(self, *, is_morning_report: bool) -> str:
        """Short directive prompt sent via stdin."""
        base = (
            "Run your ego cycle. Review the operational context above, "
            "check your open threads, and use your MCP tools to verify "
            "any beliefs before proposing actions. End with valid JSON "
            "matching the ego output schema."
        )
        if is_morning_report:
            base += (
                "\n\nThis is a MORNING REPORT cycle. Include the morning_report "
                "field with your daily briefing for the user."
            )
        return base

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
                result = json.loads(text[first_brace:last_brace + 1])
                if isinstance(result, dict):
                    return _validate_output(result)
            except json.JSONDecodeError:
                pass

        logger.error(
            "Failed to parse ego output (length=%d): %.200s...",
            len(text), text,
        )
        return None

    # -- Helpers -----------------------------------------------------------

    async def _process_proposals(
        self,
        proposals: list[dict],
        cycle_id: str,
    ) -> None:
        """Create proposal batch and send digest to Telegram."""
        try:
            batch_id, ids = await self._proposals.create_batch(
                proposals, cycle_id=cycle_id,
            )
            logger.info(
                "Created proposal batch %s with %d proposals",
                batch_id, len(ids),
            )
            delivery = await self._proposals.send_digest(batch_id)
            if delivery:
                logger.info("Ego digest sent (delivery_id=%s)", delivery)
        except Exception:
            logger.error("Failed to process ego proposals", exc_info=True)

    async def _check_budget(self) -> bool:
        """True if daily ego spend is under the budget cap."""
        try:
            daily = await ego_crud.daily_ego_cost(self._db)
            if daily >= self._config.daily_budget_cap_usd:
                logger.warning(
                    "Ego daily spend $%.2f exceeds cap $%.2f",
                    daily, self._config.daily_budget_cap_usd,
                )
                return False
        except Exception:
            logger.warning("Budget check failed — allowing cycle", exc_info=True)
        return True


_VALID_URGENCIES = frozenset({"low", "normal", "high", "critical"})


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
    return data
