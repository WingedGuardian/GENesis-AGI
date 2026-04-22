"""Init function: direct session spawner.

Creates a dedicated CCInvoker + DirectSessionRunner and wires the MCP
tools.  Placed after ``cc_relay`` in bootstrap (needs session_manager)
but accesses ``outreach_pipeline`` lazily via runtime ref.

Also starts a background poll loop that claims items from the
``direct_session_queue`` table and dispatches them to the runner.
This decouples session lifecycle from MCP server processes — sessions
outlive the calling CC session.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.cc.direct_session import DirectSessionRunner
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")

_POLL_INTERVAL_S = 5


async def _direct_session_poll(runner: DirectSessionRunner, db) -> None:
    """Poll direct_session_queue for pending items, dispatch to runner."""
    from genesis.cc.direct_session import DirectSessionRequest
    from genesis.cc.types import CCModel, EffortLevel
    from genesis.db.crud import direct_session_queue as dsq

    # Crash recovery: reset items that were claimed but never dispatched
    # before a crash/restart. Items in 'dispatched' status are intentionally
    # excluded — they have a running session tracked in cc_sessions.
    try:
        recovered = await dsq.recover_stale_claims(db)
        if recovered:
            logger.info("Recovered %d stale direct session queue claims", recovered)
    except Exception:
        logger.error("Direct session stale claim recovery failed", exc_info=True)

    while True:
        try:
            await asyncio.sleep(_POLL_INTERVAL_S)
            row = await dsq.claim_next(db)
            if row is None:
                continue

            queue_id = row["id"]
            try:
                payload = json.loads(row["payload_json"])
                request = DirectSessionRequest(
                    prompt=payload["prompt"],
                    profile=payload.get("profile", "observe"),
                    model=CCModel(payload.get("model", "sonnet")),
                    effort=EffortLevel(payload.get("effort", "high")),
                    timeout_s=payload.get("timeout_s", 900),
                    notify=payload.get("notify", True),
                    notify_on_failure_only=payload.get("notify_on_failure_only", False),
                    caller_context=payload.get("caller_context"),
                )
                session_id = await runner.spawn(request)
                await dsq.mark_dispatched(db, queue_id, session_id)
                logger.info(
                    "Dispatched direct session %s → %s (profile=%s)",
                    queue_id,
                    session_id,
                    request.profile,
                )
            except Exception as exc:
                await dsq.mark_failed(db, queue_id, str(exc))
                logger.error(
                    "Failed to dispatch direct session %s: %s",
                    queue_id,
                    exc,
                    exc_info=True,
                )

        except asyncio.CancelledError:
            break
        except Exception:
            logger.error("Direct session poll loop error", exc_info=True)


async def init(rt: GenesisRuntime) -> None:
    """Initialize the direct session spawner and queue poll loop."""
    if rt._session_manager is None:
        logger.warning("Session manager not available — skipping direct session")
        return

    if rt._db is None:
        logger.warning("DB not available — skipping direct session")
        return

    try:
        from genesis.cc.direct_session import DirectSessionRunner
        from genesis.cc.invoker import CCInvoker
        from genesis.cc.session_config import SessionConfigBuilder
        from genesis.util.tasks import tracked_task

        # Dedicated invoker for the runner — avoids _active_proc race
        # under Semaphore(2) with the shared invoker (architect finding #5).
        # Uses simpler callbacks: status changes log only, no resilience
        # state machine wiring (the shared invoker handles that).
        async def _on_status_change(status_str: str) -> None:
            logger.info("Direct session invoker status: %s", status_str)

        invoker = CCInvoker(on_cc_status_change=_on_status_change)

        runner = DirectSessionRunner(
            invoker=invoker,
            session_manager=rt._session_manager,
            config_builder=SessionConfigBuilder(),
            runtime=rt,
        )
        rt._direct_session_runner = runner

        # Wire MCP tools (pass db so they can enqueue, runner for active_count)
        from genesis.mcp.health.direct_session_tools import init_direct_session_tools

        init_direct_session_tools(db=rt._db, runner=runner)

        # Start background poll loop to claim and dispatch queued sessions
        rt._direct_session_poll = tracked_task(
            _direct_session_poll(runner, rt._db),
            name="direct-session-poll",
        )

        logger.info("Direct session spawner initialized (with queue poll loop)")

    except Exception:
        logger.exception("Failed to initialize direct session spawner")
