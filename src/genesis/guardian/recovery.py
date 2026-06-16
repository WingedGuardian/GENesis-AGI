"""Recovery engine — HOST-SIDE. Executes recovery actions with verification.

Recovery actions in escalation order:
1. RESTART_SERVICES  — systemctl restart genesis-bridge
2. IO_TRIAGE         — kill top I/O consumer (one per cycle)
3. RESOURCE_CLEAR    — clear /tmp, reclaim page cache, restart
4. REVERT_CODE       — git stash && git revert HEAD, restart
5. RESTART_CONTAINER — incus restart genesis
6. SNAPSHOT_ROLLBACK — incus snapshot restore genesis {last_healthy}
7. ESCALATE          — alert user, stop automated recovery

Each step: pre-alert → pre-snapshot → execute → wait → verify → post-alert.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from genesis.guardian._subprocess import run_subprocess as _run_subprocess
from genesis.guardian.alert.base import Alert, AlertSeverity
from genesis.guardian.alert.dispatcher import AlertDispatcher
from genesis.guardian.config import GuardianConfig
from genesis.guardian.diagnosis import DiagnosisResult, RecoveryAction
from genesis.guardian.health_signals import collect_all_signals
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

        # Check exponential backoff before proceeding
        backoff_s = self._sm.recovery_backoff_remaining_s(action.value)
        if backoff_s > 0:
            attempts = (
                self._sm.state.io_triage_attempts
                if action == RecoveryAction.IO_TRIAGE
                else self._sm.state.recovery_attempts
            )
            logger.warning(
                "Recovery backoff: %.0fs remaining before attempt %d — deferring",
                backoff_s, attempts + 1,
            )
            return RecoveryResult(
                action=action,
                success=False,
                detail=f"Recovery deferred: {backoff_s:.0f}s backoff remaining",
                duration_s=0.0,
            )

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

        # Pre-recovery snapshot (gated on config flag and disk space)
        snap_history = self._sm.state.snapshot_size_history
        if (
            action != RecoveryAction.SNAPSHOT_ROLLBACK
            and self._config.snapshots.take_pre_recovery
            and await self._snapshots.safe_to_snapshot(snap_history)
        ):
            snap_name = await self._snapshots.take(
                label="pre-recovery",
                snapshot_size_history=snap_history,
            )
            if snap_name:
                logger.info("Pre-recovery snapshot: %s", snap_name)

        # Execute the action
        try:
            success, detail = await self._execute_action(action)
        except Exception as exc:
            logger.error("Recovery action %s failed: %s", action, exc, exc_info=True)
            success = False
            detail = str(exc)

        # Record attempt timestamp for backoff tracking
        self._sm.record_recovery_attempt(action.value)

        duration = (datetime.now(UTC) - t0).total_seconds()

        if success:
            # Wait for services to stabilize
            await asyncio.sleep(self._config.recovery.verification_delay_s)

            # Verify recovery via health probes
            snapshot = await collect_all_signals(self._config)
            if snapshot.all_alive:
                self._sm.set_recovered()
                self._sm.clear_down_alert_sent()  # GUARD-R2-01: episode over
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
        elif action == RecoveryAction.IO_TRIAGE:
            return await self._io_triage(container)
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

    async def _io_triage(self, container: str) -> tuple[bool, str]:
        """Kill the top I/O consumer. One process per cycle — reassess after.

        Checks PSI trend before acting: if pressure is already dropping,
        stands down and lets the system recover naturally.
        Never touches io.max (host safety boundary).
        """
        import asyncio

        from genesis.guardian.cgroup_ops import (
            find_top_io_pids,
            find_top_io_pids_rate,
            kill_pid,
            read_io_pressure,
        )

        # 1. Collect diagnostics — rate-based ranking (500ms delta sample)
        #    identifies the actual current I/O offender, not just the
        #    process with the highest cumulative lifetime total.
        top_pids = await asyncio.to_thread(find_top_io_pids_rate, container, 5)
        if not top_pids:
            # Fallback to cumulative if rate sampling fails (all PIDs gone)
            top_pids = find_top_io_pids(container, top_n=5)
            if not top_pids:
                return False, "No I/O consuming processes found in container cgroup"

        # Log all candidates for observability
        for entry in top_pids:
            if "total_rate" in entry:
                logger.info(
                    "IO_TRIAGE candidate: PID %d (%s) "
                    "rate=%.0f bytes/s (cumulative: read=%d write=%d)",
                    entry["pid"], entry["comm"], entry["total_rate"],
                    entry.get("read_bytes_cumulative", 0),
                    entry.get("write_bytes_cumulative", 0),
                )
            else:
                logger.info(
                    "IO_TRIAGE candidate (cumulative fallback): PID %d (%s) "
                    "read=%d write=%d total=%d bytes",
                    entry["pid"], entry["comm"],
                    entry["read_bytes"], entry["write_bytes"],
                    entry["total_bytes"],
                )

        # 2. Assess PSI trend — is pressure accelerating or recovering?
        pressure = read_io_pressure(container)
        if pressure:
            avg10 = pressure.get("full_avg10", 0)
            avg60 = pressure.get("full_avg60", 0)
            if avg10 < avg60:
                # Pressure is dropping — stand down
                return True, (
                    f"I/O pressure recovering (avg10={avg10:.1f}%, "
                    f"avg60={avg60:.1f}%) — standing down"
                )

        # 3. Kill top consumer only (one per cycle)
        target = top_pids[0]
        killed = await kill_pid(target["pid"], container=container)
        if not killed:
            return False, (
                f"Failed to kill PID {target['pid']} ({target['comm']})"
            )

        # Report rate if available, else cumulative
        if "total_rate" in target:
            io_detail = f"rate={target['total_rate']:.0f} bytes/s"
        else:
            io_detail = f"total_bytes={target['total_bytes']}"

        return True, (
            f"Killed top I/O consumer: PID {target['pid']} "
            f"({target['comm']}) {io_detail}"
        )

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
        attempts = len(diagnosis.actions_taken)
        actions_str = ", ".join(diagnosis.actions_taken) if diagnosis.actions_taken else "none"
        cc_note = ""
        if diagnosis.cc_failure_reason:
            cc_note = f"CC failure: {diagnosis.cc_failure_reason}\n"
        cname = self._config.container_name

        await self._dispatcher.send(Alert(
            severity=AlertSeverity.EMERGENCY,
            title="Genesis down — all automated recovery failed",
            body=(
                f"Cause: {diagnosis.likely_cause}\n"
                f"Confidence: {diagnosis.confidence_pct}%\n"
                f"{cc_note}"
                f"Actions tried ({attempts}): {actions_str}\n\n"
                "IMMEDIATE ACTION REQUIRED:\n"
                "1. SSH to host VM\n"
                "2. Check Guardian log: ~/.local/state/genesis-guardian/guardian.log\n"
                f"3. Check container: incus exec {cname} -- "
                f"su - ubuntu -c 'journalctl --user -n 200'\n"
                f"4. Manual restart: incus exec {cname} -- "
                f"su - ubuntu -c 'systemctl --user restart genesis-server'"
            ),
            likely_cause=diagnosis.likely_cause,
            failed_probes=diagnosis.evidence,
        ))
        return RecoveryResult(
            action=RecoveryAction.ESCALATE,
            success=True,  # escalation itself always "succeeds"
            detail="Escalated to user",
            duration_s=0.0,
        )
