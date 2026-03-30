"""Recovery engine — executes recovery actions with verification.

Recovery actions in escalation order:
1. RESTART_SERVICES  — systemctl restart genesis-bridge
2. RESOURCE_CLEAR    — clear /tmp, reclaim page cache, restart
3. REVERT_CODE       — git stash && git revert HEAD, restart
4. RESTART_CONTAINER — incus restart genesis
5. SNAPSHOT_ROLLBACK — incus snapshot restore genesis {last_healthy}
6. ESCALATE          — alert user, stop automated recovery

Each step: pre-alert → pre-snapshot → execute → wait → verify → post-alert.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from genesis.guardian.alert.base import Alert, AlertSeverity
from genesis.guardian.alert.dispatcher import AlertDispatcher
from genesis.guardian.config import GuardianConfig
from genesis.guardian.diagnosis import DiagnosisResult, RecoveryAction
from genesis.guardian.health_signals import _run_subprocess, collect_all_signals
from genesis.guardian.snapshots import SnapshotManager
from genesis.guardian.state_machine import ConfirmationStateMachine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryResult:
    """Result of a recovery attempt."""

    action: RecoveryAction
    success: bool
    detail: str
    duration_s: float


class RecoveryEngine:
    """Execute recovery actions with pre/post verification."""

    def __init__(
        self,
        config: GuardianConfig,
        state_machine: ConfirmationStateMachine,
        snapshots: SnapshotManager,
        dispatcher: AlertDispatcher,
    ) -> None:
        self._config = config
        self._sm = state_machine
        self._snapshots = snapshots
        self._dispatcher = dispatcher

    async def execute(self, diagnosis: DiagnosisResult) -> RecoveryResult:
        """Execute the recommended recovery action."""
        action = diagnosis.recommended_action
        t0 = datetime.now(UTC)

        if action == RecoveryAction.ESCALATE:
            return await self._escalate(diagnosis)

        # Pre-recovery alert
        await self._dispatcher.send(Alert(
            severity=AlertSeverity.CRITICAL,
            title=f"Attempting recovery: {action.value}",
            body=f"Cause: {diagnosis.likely_cause}\n"
                 f"Confidence: {diagnosis.confidence_pct}%",
            likely_cause=diagnosis.likely_cause,
            proposed_action=action.value,
        ))

        # Mark state as recovering
        self._sm.set_recovering()

        # Pre-recovery snapshot (unless we're rolling back TO a snapshot)
        if action != RecoveryAction.SNAPSHOT_ROLLBACK:
            snap_name = await self._snapshots.take(label="pre-recovery")
            if snap_name:
                logger.info("Pre-recovery snapshot: %s", snap_name)

        # Execute the action
        try:
            success, detail = await self._execute_action(action)
        except Exception as exc:
            logger.error("Recovery action %s failed: %s", action, exc, exc_info=True)
            success = False
            detail = str(exc)

        duration = (datetime.now(UTC) - t0).total_seconds()

        if success:
            # Wait for services to stabilize
            await asyncio.sleep(self._config.recovery.verification_delay_s)

            # Verify recovery via health probes
            snapshot = await collect_all_signals(self._config)
            if snapshot.all_alive:
                self._sm.set_recovered()
                await self._dispatcher.send(Alert(
                    severity=AlertSeverity.INFO,
                    title=f"Recovery successful: {action.value}",
                    body=f"Genesis is back online after {duration:.0f}s.",
                    duration_s=duration,
                ))
            else:
                # Recovery action succeeded but system still unhealthy
                failed = [s.name for s in snapshot.failed_signals]
                detail = f"Action completed but probes still failing: {', '.join(failed)}"
                success = False
                self._sm.set_confirmed_dead()
                logger.warning("Recovery verification failed: %s", detail)
        else:
            self._sm.set_confirmed_dead()
            await self._dispatcher.send(Alert(
                severity=AlertSeverity.CRITICAL,
                title=f"Recovery failed: {action.value}",
                body=detail,
                duration_s=duration,
            ))

        return RecoveryResult(
            action=action,
            success=success,
            detail=detail,
            duration_s=duration,
        )

    async def _execute_action(
        self, action: RecoveryAction,
    ) -> tuple[bool, str]:
        """Execute a single recovery action. Returns (success, detail)."""
        container = self._config.container_name

        if action == RecoveryAction.RESTART_SERVICES:
            return await self._restart_services(container)
        elif action == RecoveryAction.RESOURCE_CLEAR:
            return await self._resource_clear(container)
        elif action == RecoveryAction.REVERT_CODE:
            return await self._revert_code(container)
        elif action == RecoveryAction.RESTART_CONTAINER:
            return await self._restart_container(container)
        elif action == RecoveryAction.SNAPSHOT_ROLLBACK:
            return await self._snapshot_rollback()
        else:
            return False, f"Unknown action: {action}"

    async def _restart_services(self, container: str) -> tuple[bool, str]:
        """Restart genesis-bridge service inside the container."""
        rc, stdout, stderr = await _run_subprocess(
            "incus", "exec", container, "--",
            "su", "-", "ubuntu", "-c",
            "systemctl --user restart genesis-bridge",
            timeout=30.0,
        )
        if rc != 0:
            return False, f"systemctl restart failed: {stderr}"
        return True, "genesis-bridge restarted"

    async def _resource_clear(self, container: str) -> tuple[bool, str]:
        """Clear /tmp and reclaim page cache, then restart services."""
        # Clear /tmp (preserve essential sockets)
        rc, _, stderr = await _run_subprocess(
            "incus", "exec", container, "--",
            "find", "/tmp", "-type", "f",
            "-not", "-name", "*.sock",
            "-mmin", "+5", "-delete",
            timeout=30.0,
        )
        if rc != 0:
            logger.warning("tmp cleanup had errors: %s", stderr)

        # Reclaim page cache
        rc, _, _ = await _run_subprocess(
            "incus", "exec", container, "--",
            "bash", "-c", "sync && echo 1 > /proc/sys/vm/drop_caches",
            timeout=10.0,
        )

        # Restart services
        return await self._restart_services(container)

    async def _revert_code(self, container: str) -> tuple[bool, str]:
        """Stash uncommitted changes and revert last commit, then restart."""
        # Stash any uncommitted work
        rc, _, _ = await _run_subprocess(
            "incus", "exec", container, "--",
            "su", "-", "ubuntu", "-c",
            "cd ~/genesis && git stash",
            timeout=15.0,
        )

        # Revert the last commit
        rc, stdout, stderr = await _run_subprocess(
            "incus", "exec", container, "--",
            "su", "-", "ubuntu", "-c",
            "cd ~/genesis && git revert --no-edit HEAD",
            timeout=30.0,
        )
        if rc != 0:
            return False, f"git revert failed: {stderr}"

        # Restart services after code change
        svc_ok, svc_detail = await self._restart_services(container)
        return svc_ok, f"Code reverted, {svc_detail}"

    async def _restart_container(self, container: str) -> tuple[bool, str]:
        """Restart the entire container."""
        rc, stdout, stderr = await _run_subprocess(
            "incus", "restart", container, "--timeout", "30",
            timeout=60.0,
        )
        if rc != 0:
            return False, f"incus restart failed: {stderr}"
        return True, "Container restarted"

    async def _snapshot_rollback(self) -> tuple[bool, str]:
        """Rollback to the last healthy snapshot."""
        healthy = await self._snapshots.get_latest_healthy()
        if not healthy:
            return False, "No healthy snapshot available for rollback"

        ok = await self._snapshots.restore(healthy)
        if not ok:
            return False, f"Failed to restore snapshot {healthy}"
        return True, f"Restored snapshot {healthy}"

    async def _escalate(self, diagnosis: DiagnosisResult) -> RecoveryResult:
        """Send escalation alert — no automated recovery."""
        await self._dispatcher.send(Alert(
            severity=AlertSeverity.EMERGENCY,
            title="Recovery escalated — manual intervention required",
            body=f"Cause: {diagnosis.likely_cause}\n"
                 f"Confidence: {diagnosis.confidence_pct}%\n"
                 f"Reasoning: {diagnosis.reasoning}\n\n"
                 "Automated recovery has been exhausted or confidence is too low. "
                 "Manual investigation required.",
            likely_cause=diagnosis.likely_cause,
            failed_probes=diagnosis.evidence,
        ))
        return RecoveryResult(
            action=RecoveryAction.ESCALATE,
            success=True,  # escalation itself always "succeeds"
            detail="Escalated to user",
            duration_s=0.0,
        )
