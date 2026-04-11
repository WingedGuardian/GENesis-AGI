"""Init degradation recorder — observation + event for partial subsystem failures.

Called from ``runtime/init/*`` submodules when a non-critical component fails
to wire during bootstrap. Creates a dedup'd ``init_degradation`` observation
(visible in dashboard / morning report) and emits an event on the event bus
(visible in event bus / logs).

Pure module-level helper. Takes ``db`` and ``event_bus`` explicitly so it has
zero coupling to ``GenesisRuntime`` — that's what makes it cheap to extract
out of ``_core.py`` in the runtime split.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

logger = logging.getLogger("genesis.runtime")


async def record_init_degradation(
    db,
    event_bus,
    subsystem: str,
    component: str,
    error: str,
    *,
    severity: str = "warning",
) -> None:
    """Record a subsystem init degradation as an observation + event.

    Called from init modules when a non-critical component fails to wire.
    Creates an observation (visible in dashboard/morning report) and emits
    an event (visible in event bus/logs).
    """
    priority = "high" if severity == "error" else "medium"
    content_text = f"[{subsystem}] {component}: {error}"
    if db is not None:
        try:
            import uuid

            from genesis.db.crud import observations

            # Dedup: skip if an unresolved init_degradation for this component exists
            existing = await db.execute(
                "SELECT 1 FROM observations WHERE source = 'bootstrap' "
                "AND type = 'init_degradation' AND content = ? "
                "AND resolved_at IS NULL LIMIT 1",
                (content_text,),
            )
            if await existing.fetchone():
                logger.debug("Init degradation already recorded for %s.%s", subsystem, component)
            else:
                await observations.create(
                    db,
                    id=str(uuid.uuid4()),
                    source="bootstrap",
                    type="init_degradation",
                    content=content_text,
                    priority=priority,
                    created_at=datetime.now(UTC).isoformat(),
                    category="infrastructure",
                )
        except Exception:
            logger.warning("Failed to record init degradation observation", exc_info=True)
    if event_bus is not None:
        try:
            from genesis.observability.types import Severity, Subsystem

            sev = Severity.ERROR if severity == "error" else Severity.WARNING
            await event_bus.emit(
                Subsystem.INFRA,
                sev,
                f"init.{subsystem}.degraded",
                f"{component}: {error}",
            )
        except Exception:
            logger.warning("Failed to emit init degradation event", exc_info=True)
