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
# Re-run stale-claim recovery roughly every 60s inside the poll loop (not just
# once at startup): a claim made <120s before a fast restart is missed by the
# one-shot startup pass and would otherwise stay 'claimed' until a later restart.
_RECOVERY_EVERY_N_POLLS = max(1, 60 // _POLL_INTERVAL_S)


async def _recover_stale_claims(db) -> None:
    """Reset items stuck in 'claimed' (claimed but never dispatched) back to pending.

    Items in 'dispatched' status are intentionally excluded — they track a running
    session in cc_sessions. Because 'claimed' is a sub-second transient between
    claim_next() and mark_dispatched(), this is safe to run repeatedly on a live
    server: it only frees a claim orphaned by a crash mid-dispatch, never an
    in-flight session. Best-effort — a failure here must not kill the poll loop.
    """
    from genesis.db.crud import direct_session_queue as dsq

    try:
        recovered = await dsq.recover_stale_claims(db)
        if recovered:
            logger.info("Recovered %d stale direct session queue claims", recovered)
    except Exception:
        logger.error("Direct session stale claim recovery failed", exc_info=True)


async def _direct_session_poll(runner: DirectSessionRunner, db) -> None:
    """Poll direct_session_queue for pending items, dispatch to runner."""
    from genesis.cc.direct_session import DirectSessionRequest
    from genesis.cc.types import CCModel, DeliveryMode, EffortLevel
    from genesis.db.crud import direct_session_queue as dsq

    # Crash recovery on boot: reset claims orphaned before a crash/restart.
    await _recover_stale_claims(db)

    poll_count = 0
    while True:
        try:
            await asyncio.sleep(_POLL_INTERVAL_S)

            # Periodic re-run so a claim younger than the 120s floor at a fast
            # restart doesn't stay stuck. Runs BEFORE the capacity check so a
            # saturated runner can't starve recovery.
            poll_count += 1
            if poll_count % _RECOVERY_EVERY_N_POLLS == 0:
                await _recover_stale_claims(db)

            # Don't over-claim: if runner is at capacity, wait for a slot.
            # spawn() returns immediately but sessions queue behind the
            # internal Semaphore — claiming more just wastes queue items.
            if runner.active_count() >= runner._MAX_CONCURRENT:
                continue

            row = await dsq.claim_next(db)
            if row is None:
                continue

            queue_id = row["id"]
            try:
                payload = json.loads(row["payload_json"])
                delivery_mode_raw = payload.get("delivery_mode")
                request = DirectSessionRequest(
                    prompt=payload["prompt"],
                    profile=payload.get("profile", "observe"),
                    model=CCModel(payload.get("model", "sonnet")),
                    effort=EffortLevel(payload.get("effort", "high")),
                    timeout_s=payload.get("timeout_s", 3600),
                    notify=payload.get("notify", True),
                    notify_on_failure_only=payload.get("notify_on_failure_only", False),
                    caller_context=payload.get("caller_context"),
                    roster_model=payload.get("roster_model"),
                    # Origin-delivery (added by the deliver_to_origin dispatch path);
                    # absent on legacy rows → None → derived legacy behavior.
                    origin_session_id=payload.get("origin_session_id"),
                    delivery_mode=(DeliveryMode(delivery_mode_raw) if delivery_mode_raw else None),
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

        # Inject protected paths for background session prompt hardening
        if getattr(rt, "_protected_paths", None) is not None:
            runner.set_protected_paths(rt._protected_paths)
            logger.info("ProtectedPathRegistry wired into DirectSessionRunner")

        # Inject post-execution auditor for autonomy feedback loop
        if getattr(rt, "_post_execution_auditor", None) is not None:
            runner.set_auditor(rt._post_execution_auditor)
            logger.info("PostExecutionAuditor wired into DirectSessionRunner")

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
