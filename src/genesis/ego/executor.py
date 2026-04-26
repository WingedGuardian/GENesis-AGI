"""EgoProposalExecutor — dispatches approved ego proposals.

Polls every 2 minutes for proposals with status='approved' and
dispatches them based on action_type:

- investigate → DirectSessionRunner (observe profile)
- dispatch   → DirectSessionRunner (research profile)
- maintenance → SurplusQueue (or follow-up fallback)
- outreach   → OutreachPipeline
- config     → follow-up for user review
- (unknown)  → follow-up for ego judgment

Each execution updates the proposal status to 'executed' (with the
session/task ID in user_response) or 'failed' (with the error). A
follow-up is created for every execution to track outcomes.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from genesis.db.crud import ego as ego_crud
from genesis.db.crud import follow_ups as follow_up_crud

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

_POLL_INTERVAL_MINUTES = 2
_MAX_PER_TICK = 3


class EgoProposalExecutor:
    """Polls approved ego proposals and dispatches them.

    Graceful degradation: when a dependency (DirectSessionRunner,
    SurplusQueue, OutreachPipeline) is unavailable, the corresponding
    action type fails with a clear error and the executor continues
    with remaining proposals.
    """

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        direct_session_runner: Any | None = None,
        surplus_queue: Any | None = None,
        outreach_pipeline: Any | None = None,
    ) -> None:
        self._db = db
        self._runner = direct_session_runner
        self._surplus_queue = surplus_queue
        self._outreach = outreach_pipeline
        self._scheduler = AsyncIOScheduler()
        self._running = False

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Register APScheduler job and start polling."""
        self._scheduler.add_job(
            self._on_tick,
            IntervalTrigger(minutes=_POLL_INTERVAL_MINUTES),
            id="ego_proposal_executor",
            max_instances=1,
            misfire_grace_time=120,
        )
        self._scheduler.start()
        self._running = True
        logger.info("Ego proposal executor started (poll=%dm)", _POLL_INTERVAL_MINUTES)

    async def stop(self) -> None:
        """Clean shutdown."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        self._running = False
        logger.info("Ego proposal executor stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    # -- Tick ----------------------------------------------------------------

    async def _on_tick(self) -> None:
        """Poll for approved proposals and dispatch."""
        # Check Genesis pause state
        try:
            from genesis.runtime import GenesisRuntime

            rt = GenesisRuntime.instance()
            if rt and rt.paused:
                return
        except Exception:
            pass

        try:
            proposals = await ego_crud.list_proposals(
                self._db, status="approved", limit=_MAX_PER_TICK,
            )
        except Exception:
            logger.error("Failed to query approved proposals", exc_info=True)
            return

        if not proposals:
            return

        logger.info("Executor found %d approved proposal(s)", len(proposals))
        for proposal in proposals:
            await self._dispatch_one(proposal)

    async def _dispatch_one(self, proposal: dict) -> None:
        """Dispatch a single approved proposal. Never raises."""
        proposal_id = proposal.get("id", "?")
        action_type = proposal.get("action_type", "")

        try:
            handler_name = _ACTION_HANDLERS.get(action_type, "_handle_unknown")
            handler = getattr(self, handler_name)
            result_id = await handler(proposal)

            await ego_crud.execute_proposal(
                self._db,
                proposal_id,
                status="executed",
                user_response=result_id,
            )

            # Create follow-up for tracking
            content_short = (proposal.get("content") or "")[:200]
            await follow_up_crud.create(
                self._db,
                content=f"Track ego proposal {proposal_id}: {content_short}",
                source="ego_executor",
                strategy="ego_judgment",
                reason=f"Dispatched as {action_type} → {result_id}",
                priority=_urgency_to_priority(proposal.get("urgency", "normal")),
            )

            logger.info(
                "Executed proposal %s [%s] → %s",
                proposal_id, action_type, result_id,
            )
        except Exception as exc:
            logger.error(
                "Failed to execute proposal %s [%s]: %s",
                proposal_id, action_type, exc, exc_info=True,
            )
            try:
                await ego_crud.execute_proposal(
                    self._db,
                    proposal_id,
                    status="failed",
                    user_response=f"{type(exc).__name__}: {exc}"[:500],
                )
            except Exception:
                logger.error(
                    "Failed to mark proposal %s as failed",
                    proposal_id, exc_info=True,
                )

    # -- Action type handlers ------------------------------------------------

    async def _handle_investigate(self, proposal: dict) -> str:
        """Spawn a research/observe CC session."""
        if self._runner is None:
            raise RuntimeError("DirectSessionRunner not available")

        from genesis.cc.direct_session import DirectSessionRequest
        from genesis.cc.types import CCModel, EffortLevel

        request = DirectSessionRequest(
            prompt=_build_investigate_prompt(proposal),
            profile="observe",
            model=CCModel.SONNET,
            effort=EffortLevel.HIGH,
            notify=True,
            source_tag="ego_proposal_executor",
            caller_context=f"ego_proposal:{proposal['id']}",
        )
        session_id = await self._runner.spawn(request)
        return f"session:{session_id}"

    async def _handle_dispatch(self, proposal: dict) -> str:
        """Spawn an interactive CC session."""
        if self._runner is None:
            raise RuntimeError("DirectSessionRunner not available")

        from genesis.cc.direct_session import DirectSessionRequest
        from genesis.cc.types import CCModel, EffortLevel

        request = DirectSessionRequest(
            prompt=_build_dispatch_prompt(proposal),
            profile="research",
            model=CCModel.SONNET,
            effort=EffortLevel.HIGH,
            notify=True,
            source_tag="ego_proposal_executor",
            caller_context=f"ego_proposal:{proposal['id']}",
        )
        session_id = await self._runner.spawn(request)
        return f"session:{session_id}"

    async def _handle_maintenance(self, proposal: dict) -> str:
        """Enqueue to surplus queue or create follow-up fallback."""
        if self._surplus_queue is not None:
            from genesis.surplus.types import ComputeTier, TaskType

            task_id = await self._surplus_queue.enqueue(
                task_type=TaskType.INFRASTRUCTURE_MONITOR,
                compute_tier=ComputeTier.FREE_API,
                priority=0.6,
                drive_alignment="preservation",
                payload=json.dumps({
                    "source": "ego_proposal",
                    "proposal_id": proposal["id"],
                    "content": (proposal.get("content") or "")[:500],
                }),
            )
            return f"surplus:{task_id}"

        # Fallback: create follow-up
        fid = await follow_up_crud.create(
            self._db,
            content=proposal.get("content", ""),
            source="ego_executor",
            strategy="ego_judgment",
            reason=proposal.get("rationale", ""),
            priority=_urgency_to_priority(proposal.get("urgency", "normal")),
        )
        return f"follow_up:{fid}"

    async def _handle_outreach(self, proposal: dict) -> str:
        """Create an outreach request."""
        if self._outreach is None:
            raise RuntimeError("OutreachPipeline not available")

        from genesis.outreach.types import OutreachCategory, OutreachRequest

        result = await self._outreach.submit(OutreachRequest(
            category=OutreachCategory.DIGEST,
            topic="ego_proposal_outreach",
            context=proposal.get("content", ""),
            salience_score=0.7,
        ))
        return f"outreach:{result.outreach_id}"

    async def _handle_config(self, proposal: dict) -> str:
        """Create a follow-up for user to review config change."""
        fid = await follow_up_crud.create(
            self._db,
            content=f"Config change proposed by ego: {proposal.get('content', '')}",
            source="ego_executor",
            strategy="user_input_needed",
            reason=proposal.get("rationale", ""),
            priority="high",
        )
        return f"follow_up:{fid}"

    async def _handle_unknown(self, proposal: dict) -> str:
        """Fallback: create a follow-up for unknown action types."""
        fid = await follow_up_crud.create(
            self._db,
            content=(
                f"Ego proposal (unknown type {proposal.get('action_type')!r}): "
                f"{proposal.get('content', '')}"
            ),
            source="ego_executor",
            strategy="ego_judgment",
            reason=proposal.get("rationale", ""),
            priority=_urgency_to_priority(proposal.get("urgency", "normal")),
        )
        return f"follow_up:{fid}"


# Action type → handler method name dispatch table
_ACTION_HANDLERS: dict[str, str] = {
    "investigate": "_handle_investigate",
    "dispatch": "_handle_dispatch",
    "maintenance": "_handle_maintenance",
    "outreach": "_handle_outreach",
    "config": "_handle_config",
}


# -- Prompt builders --------------------------------------------------------

def _build_investigate_prompt(proposal: dict) -> str:
    content = proposal.get("content", "")
    rationale = proposal.get("rationale", "")
    return (
        f"INVESTIGATION REQUEST (from ego proposal)\n\n"
        f"Task: {content}\n\n"
        f"Context: {rationale}\n\n"
        f"Instructions:\n"
        f"1. Use your MCP tools to investigate this issue\n"
        f"2. Check health status, memory, observations as needed\n"
        f"3. Summarize your findings clearly\n"
        f"4. If action is needed, describe what should be done\n"
    )


def _build_dispatch_prompt(proposal: dict) -> str:
    content = proposal.get("content", "")
    rationale = proposal.get("rationale", "")
    return (
        f"ACTION REQUEST (from ego proposal)\n\n"
        f"Task: {content}\n\n"
        f"Context: {rationale}\n\n"
        f"Instructions:\n"
        f"1. Execute this task using your available tools\n"
        f"2. Verify the result\n"
        f"3. Report what was done and the outcome\n"
    )


def _urgency_to_priority(urgency: str) -> str:
    """Map proposal urgency to follow-up priority."""
    return {
        "low": "low",
        "normal": "medium",
        "high": "high",
        "critical": "critical",
    }.get(urgency, "medium")
