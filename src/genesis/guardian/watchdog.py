"""GuardianWatchdog — CONTAINER-SIDE. Monitors Guardian health from inside the container.

Called every awareness tick (5 min). Reads the Guardian heartbeat file,
triggers SSH recovery if stale, and escalates via Telegram if recovery fails.

When Guardian is stuck in confirmed_dead (state machine won't auto-reset and
timer restarts don't help), escalates to reset-state via SSH.

Also performs code drift detection: compares container's Guardian-relevant
commit hash with the host's deployed version, alerting if they diverge.

# GROUNDWORK(guardian-bidirectional): Container-side monitoring of host Guardian
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class GuardianWatchdog:
    """Container-side Guardian health monitor with automatic recovery.

    Reads the heartbeat file written by the Guardian into the container.
    When the heartbeat is stale (>STALE_THRESHOLD_S), attempts to restart
    the Guardian timer via SSH. A cooldown prevents restart storms.

    When Guardian is stuck in confirmed_dead across multiple ticks (timer
    restarts don't help), issues a reset-state command to force the state
    machine back to healthy.
    """

    RECOVERY_COOLDOWN_S = 900   # 15 min between restart attempts
    STALE_THRESHOLD_S = 300     # 5 min = DOWN (matches probe_guardian default)
    STUCK_THRESHOLD = 2         # Consecutive ticks seeing confirmed_dead before reset
    RESET_COOLDOWN_S = 1800     # 30 min between reset attempts (conservative)

    # Paths that constitute "Guardian-relevant code" for drift detection.
    _GUARDIAN_PATHS = [
        "src/genesis/guardian", "src/genesis/util", "src/genesis/env.py",
        "src/genesis/observability", "src/genesis/db",
        "config/guardian-claude.md", "pyproject.toml",
        "scripts/install_guardian.sh", "scripts/guardian-gateway.sh",
    ]
    DRIFT_ALERT_THRESHOLD = 3   # Consecutive drifted ticks before alerting

    def __init__(
        self,
        remote,  # GuardianRemote — import avoided for loose coupling
        event_bus=None,
        outreach_queue=None,
    ) -> None:
        self._remote = remote
        self._event_bus = event_bus
        self._outreach_queue = outreach_queue
        self._sentinel = None
        self._last_recovery_at: datetime | None = None
        self._last_reset_at: datetime | None = None
        self._consecutive_stuck: int = 0
        self._drift_count: int = 0

    def _in_cooldown(self) -> bool:
        if self._last_recovery_at is None:
            return False
        elapsed = (datetime.now(UTC) - self._last_recovery_at).total_seconds()
        return elapsed < self.RECOVERY_COOLDOWN_S

    def _in_reset_cooldown(self) -> bool:
        if self._last_reset_at is None:
            return False
        elapsed = (datetime.now(UTC) - self._last_reset_at).total_seconds()
        return elapsed < self.RESET_COOLDOWN_S

    def set_sentinel(self, sentinel) -> None:
        """Inject Sentinel dispatcher for escalation on reset-state failure."""
        self._sentinel = sentinel

    async def check_and_recover(self) -> None:
        """Check Guardian heartbeat and attempt recovery if stale.

        Called from the awareness loop tick. Safe to call frequently —
        returns immediately if Guardian is healthy or cooldown is active.

        Recovery escalation:
        1. First detection: restart-timer via SSH
        2. If stuck in confirmed_dead for STUCK_THRESHOLD ticks: reset-state
        """
        from genesis.observability.health import ProbeStatus, probe_guardian

        result = await probe_guardian(guardian_remote=self._remote)

        if result.status != ProbeStatus.DOWN:
            self._consecutive_stuck = 0
            return

        staleness = result.details.get("staleness_s", 0) if result.details else 0

        # Step 1: Try restart-timer if not in cooldown
        if not self._in_cooldown():
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

        # Step 2: Check if Guardian is stuck in confirmed_dead
        await self._check_stuck_state()

        # Step 3: Check for code version drift between container and host
        await self._check_code_drift()

    async def _check_stuck_state(self) -> None:
        """Detect and recover from Guardian stuck in confirmed_dead.

        Timer restarts don't reset the state machine. If Guardian is stuck
        in confirmed_dead for STUCK_THRESHOLD consecutive ticks, issue a
        reset-state command to force it back to healthy.
        """
        try:
            status = await self._remote.status()
        except Exception:
            logger.warning("Could not query Guardian status for stuck detection", exc_info=True)
            return

        current_state = status.get("current_state", "unknown")

        if current_state in ("confirmed_dead", "recovering", "recovered"):
            self._consecutive_stuck += 1
            logger.info(
                "Guardian state is %s (consecutive stuck count: %d/%d)",
                current_state, self._consecutive_stuck, self.STUCK_THRESHOLD,
            )

            if self._consecutive_stuck >= self.STUCK_THRESHOLD and not self._in_reset_cooldown():
                logger.warning(
                    "Guardian stuck in %s for %d consecutive checks — resetting state",
                    current_state, self._consecutive_stuck,
                )
                result = await self._remote.reset_state()
                self._last_reset_at = datetime.now(UTC)

                if result.get("ok"):
                    stuck_count = self._consecutive_stuck
                    self._consecutive_stuck = 0
                    logger.info(
                        "Guardian state reset from %s to healthy — restarting timer",
                        result.get("previous_state", "unknown"),
                    )
                    await self._remote.restart()
                    if self._event_bus:
                        from genesis.observability.types import Severity, Subsystem
                        await self._event_bus.emit(
                            Subsystem.GUARDIAN, Severity.WARNING,
                            "guardian.state_reset",
                            f"Guardian stuck in {current_state} — "
                            f"reset to healthy after {stuck_count} checks",
                        )
                else:
                    logger.error(
                        "Guardian reset-state failed: %s", result.get("error", "unknown"),
                    )
                    # Dispatch Sentinel to diagnose why reset-state failed
                    if self._sentinel is not None:
                        from genesis.util.tasks import tracked_task
                        tracked_task(
                            self._sentinel.escalate_direct(
                                trigger_source="watchdog_reset_failed",
                                tier=1,
                                reason=f"Guardian reset-state failed: {result.get('error', 'unknown')}",
                                context={"current_state": current_state, "error": result.get("error")},
                            ),
                            name="sentinel-reset-failed",
                        )
        else:
            self._consecutive_stuck = 0

    async def _check_code_drift(self) -> None:
        """Detect Guardian code version drift between container and host.

        Compares the container's latest commit for Guardian-relevant paths
        against the host's deployed_commit (set by the redeploy verb).
        Alerts after DRIFT_ALERT_THRESHOLD consecutive drifted ticks.

        Best-effort: any failure silently skips. Drift detection must never
        interfere with the primary health monitoring flow.
        """
        import contextlib
        with contextlib.suppress(Exception):
            await self._check_code_drift_inner()

    async def _check_code_drift_inner(self) -> None:
        """Inner implementation of drift detection (may raise)."""
        # Get container's hash for Guardian-relevant paths
        result = subprocess.run(
            ["git", "-C", str(Path.home() / "genesis"),
             "log", "-1", "--format=%h", "--"] + self._GUARDIAN_PATHS,
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return
        container_hash = result.stdout.strip()
        if not container_hash:
            return

        # Get host's deployed hash via SSH version command
        version_info = await self._remote.version()
        if not isinstance(version_info, dict):
            return
        host_hash = version_info.get("deployed_commit", "unknown")

        if not isinstance(host_hash, str) or host_hash == "unknown":
            return  # Host hasn't been redeployed yet (pre-feature) — skip

        # Compare (short hashes — prefix match)
        if container_hash.startswith(host_hash) or host_hash.startswith(container_hash):
            if self._drift_count > 0:
                logger.info("Guardian code drift resolved (container=%s host=%s)",
                            container_hash, host_hash)
            self._drift_count = 0
            return

        # Drift detected
        self._drift_count += 1
        if self._drift_count == self.DRIFT_ALERT_THRESHOLD:
            logger.error(
                "Guardian code drift detected for %d ticks: container=%s host=%s",
                self._drift_count, container_hash, host_hash,
            )
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.GUARDIAN, Severity.ERROR,
                    "guardian.code_drift",
                    f"Guardian code version mismatch — container={container_hash} "
                    f"host={host_hash} (drifted {self._drift_count} ticks)",
                )
            if self._outreach_queue:
                await self._outreach_queue.enqueue(
                    f"⚠️ Guardian code drift: container={container_hash} "
                    f"host={host_hash}. Auto-redeploy may have failed. "
                    "Check update logs or run install_guardian.sh --non-interactive on host.",
                    priority="high",
                    source="guardian_watchdog",
                )
