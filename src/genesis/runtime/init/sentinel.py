"""Bootstrap step: initialize the Sentinel — container-side guardian.

Creates SentinelDispatcher and wires it into:
- Awareness loop (fire alarm checks every tick)
- Guardian watchdog (escalation on reset-state failure)
- Remediation registry (escalation on exhausted attempts)

Graceful degradation: if CC infrastructure is not available, the Sentinel
is initialized but will skip CC dispatches (gate checks handle this).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def init_sentinel(rt) -> None:
    """Set up the Sentinel dispatcher and wire into trigger sources."""
    try:
        from genesis.sentinel.dispatcher import SentinelDispatcher
    except ImportError:
        logger.info("genesis.sentinel not available — Sentinel disabled")
        return

    sentinel = SentinelDispatcher(
        session_manager=getattr(rt, "_session_manager", None),
        invoker=getattr(rt, "_cc_invoker", None),
        remediation_registry=getattr(rt, "_remediation_registry", None),
        db=rt._db,
        event_bus=getattr(rt, "_event_bus", None),
        health_data=getattr(rt, "_health_data", None),
        outreach_pipeline=getattr(rt, "_outreach_pipeline", None),
        approval_gate=getattr(rt, "_autonomous_cli_approval_gate", None),
    )
    rt._sentinel = sentinel

    # Wire into awareness loop
    if rt._awareness_loop:
        rt._awareness_loop.set_sentinel(sentinel)
        logger.info("Sentinel wired into awareness loop (fire alarm checks)")

    # Wire into Guardian watchdog
    guardian_watchdog = None
    if rt._awareness_loop:
        guardian_watchdog = getattr(rt._awareness_loop, "_guardian_watchdog", None)
    if guardian_watchdog is not None:
        guardian_watchdog.set_sentinel(sentinel)
        logger.info("Sentinel wired into Guardian watchdog (reset-state escalation)")

    # Wire into remediation registry
    registry = getattr(rt, "_remediation_registry", None)
    if registry is not None:
        registry.set_escalation_callback(sentinel.escalate_direct)
        logger.info("Sentinel wired as remediation escalation target")

    logger.info(
        "Sentinel initialized (state=%s, cc_available=%s)",
        sentinel.state.current_state,
        sentinel._invoker is not None,
    )
