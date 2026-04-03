"""GuardianWatchdog — CONTAINER-SIDE. Monitors Guardian health from inside the container.

Called every awareness tick (5 min). Reads the Guardian heartbeat file,
triggers SSH recovery if stale, and escalates via Telegram if recovery fails.

# GROUNDWORK(guardian-bidirectional): Container-side monitoring of host Guardian
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


class GuardianWatchdog:
    """Container-side Guardian health monitor with automatic recovery.

    Reads the heartbeat file written by the Guardian into the container.
    When the heartbeat is stale (>STALE_THRESHOLD_S), attempts to restart
    the Guardian timer via SSH. A cooldown prevents restart storms.
    """

    RECOVERY_COOLDOWN_S = 900   # 15 min between restart attempts
    STALE_THRESHOLD_S = 300     # 5 min = DOWN (matches probe_guardian default)

    def __init__(
        self,
        remote,  # GuardianRemote — import avoided for loose coupling
        event_bus=None,
        outreach_queue=None,
    ) -> None:
        self._remote = remote
        self._event_bus = event_bus
        self._outreach_queue = outreach_queue
        self._last_recovery_at: datetime | None = None

    def _in_cooldown(self) -> bool:
        if self._last_recovery_at is None:
            return False
        elapsed = (datetime.now(UTC) - self._last_recovery_at).total_seconds()
        return elapsed < self.RECOVERY_COOLDOWN_S

    async def check_and_recover(self) -> None:
        """Check Guardian heartbeat and attempt recovery if stale.

        Called from the awareness loop tick. Safe to call frequently —
        returns immediately if Guardian is healthy or cooldown is active.
        """
        from genesis.observability.health import ProbeStatus, probe_guardian

        result = await probe_guardian(guardian_remote=self._remote)

        if result.status != ProbeStatus.DOWN:
            return

        staleness = result.details.get("staleness_s", 0) if result.details else 0

        if self._in_cooldown():
            logger.debug(
                "Guardian DOWN (stale %.0fs) but recovery cooldown active", staleness,
            )
            return

        logger.warning(
            "Guardian DOWN (stale %.0fs) — attempting restart via SSH", staleness,
        )
        success = await self._remote.restart()
        self._last_recovery_at = datetime.now(UTC)

        if success:
            logger.info("Guardian restart command sent — will verify on next tick")
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.GUARDIAN, Severity.WARNING,
                    "guardian.recovery.attempted",
                    f"Guardian heartbeat stale ({staleness:.0f}s) — "
                    "restart-timer sent via SSH",
                )
        else:
            logger.error("Guardian restart failed via SSH — escalating to user")
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.GUARDIAN, Severity.ERROR,
                    "guardian.recovery.failed",
                    f"Guardian restart via SSH failed (stale {staleness:.0f}s)",
                )
            if self._outreach_queue:
                try:
                    await self._outreach_queue.enqueue(
                        f"Guardian is DOWN (heartbeat stale {staleness:.0f}s) "
                        "and SSH restart failed. Manual intervention needed.",
                        priority="high",
                        source="guardian_watchdog",
                    )
                except Exception:
                    logger.warning("Failed to queue Guardian alert", exc_info=True)
