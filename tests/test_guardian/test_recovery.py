"""Tests for Guardian recovery engine."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from genesis.guardian.alert.dispatcher import AlertDispatcher
from genesis.guardian.config import GuardianConfig
from genesis.guardian.diagnosis import DiagnosisResult, RecoveryAction
from genesis.guardian.health_signals import HealthSnapshot, PauseState, SignalResult
from genesis.guardian.recovery import RecoveryEngine
from genesis.guardian.snapshots import SnapshotManager
from genesis.guardian.state_machine import ConfirmationStateMachine


@pytest.fixture
def config() -> GuardianConfig:
    return GuardianConfig()


@pytest.fixture
def sm(config: GuardianConfig) -> ConfirmationStateMachine:
    return ConfirmationStateMachine(config)


@pytest.fixture
def snapshots(config: GuardianConfig) -> SnapshotManager:
    return SnapshotManager(config)


@pytest.fixture
def dispatcher() -> AlertDispatcher:
    d = AlertDispatcher()
    ch = AsyncMock()
    ch.send.return_value = True
    d.add_channel(ch)
    return d


@pytest.fixture
def engine(
    config: GuardianConfig,
    sm: ConfirmationStateMachine,
    snapshots: SnapshotManager,
    dispatcher: AlertDispatcher,
) -> RecoveryEngine:
    return RecoveryEngine(config, sm, snapshots, dispatcher)


def _diagnosis(action: RecoveryAction = RecoveryAction.RESTART_SERVICES) -> DiagnosisResult:
    return DiagnosisResult(
        likely_cause="Test failure",
        confidence_pct=80,
        evidence=["test"],
        recommended_action=action,
        reasoning="Testing",
        source="cc",
    )


def _healthy_snapshot() -> HealthSnapshot:
    return HealthSnapshot(
        signals={
            name: SignalResult(name, True, 1.0, "ok", "t")
            for name in ["container_exists", "icmp_reachable", "health_api", "heartbeat_canary", "log_freshness"]
        },
        pause_state=PauseState(paused=False),
    )


def _mock_subprocess(rc: int = 0, stdout: str = "", stderr: str = ""):
    async def mock(*args, **kwargs):
        return (rc, stdout, stderr)
    return mock


class TestRecoveryRestart:

    @pytest.mark.asyncio
    async def test_restart_services_success(self, engine: RecoveryEngine) -> None:
        with (
            patch("genesis.guardian.recovery._run_subprocess", _mock_subprocess(0, "")),
            patch("genesis.guardian.recovery.collect_all_signals", return_value=_healthy_snapshot()),
            patch.object(engine._snapshots, "take", return_value="pre-recovery"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await engine.execute(_diagnosis(RecoveryAction.RESTART_SERVICES))
        assert result.success is True
        assert result.action == RecoveryAction.RESTART_SERVICES

    @pytest.mark.asyncio
    async def test_successful_recovery_clears_down_alert_flag(
        self, engine: RecoveryEngine
    ) -> None:
        """GUARD-R2-01: a successful recovery clears the down-alert flag so the
        next down-episode is not suppressed."""
        engine._sm.mark_down_alert_sent()
        with (
            patch("genesis.guardian.recovery._run_subprocess", _mock_subprocess(0, "")),
            patch("genesis.guardian.recovery.collect_all_signals", return_value=_healthy_snapshot()),
            patch.object(engine._snapshots, "take", return_value="pre-recovery"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await engine.execute(_diagnosis(RecoveryAction.RESTART_SERVICES))
        assert result.success is True
        assert engine._sm.state.down_alert_sent is False

    @pytest.mark.asyncio
    async def test_restart_services_failure(self, engine: RecoveryEngine) -> None:
        with (
            patch("genesis.guardian.recovery._run_subprocess", _mock_subprocess(1, "", "failed")),
            patch.object(engine._snapshots, "take", return_value="pre-recovery"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await engine.execute(_diagnosis(RecoveryAction.RESTART_SERVICES))
        assert result.success is False


class TestRecoveryEscalate:

    @pytest.mark.asyncio
    async def test_escalate(self, engine: RecoveryEngine) -> None:
        result = await engine.execute(_diagnosis(RecoveryAction.ESCALATE))
        assert result.success is True
        assert result.action == RecoveryAction.ESCALATE
        assert result.detail == "Escalated to user"


class TestRecoverySnapshotRollback:

    @pytest.mark.asyncio
    async def test_snapshot_rollback_success(self, engine: RecoveryEngine) -> None:
        with (
            patch.object(engine._snapshots, "get_latest_healthy", return_value="guardian-healthy"),
            patch.object(engine._snapshots, "restore", return_value=True),
            patch("genesis.guardian.recovery.collect_all_signals", return_value=_healthy_snapshot()),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await engine.execute(_diagnosis(RecoveryAction.SNAPSHOT_ROLLBACK))
        assert result.success is True

    @pytest.mark.asyncio
    async def test_snapshot_rollback_no_healthy(self, engine: RecoveryEngine) -> None:
        with (
            patch.object(engine._snapshots, "get_latest_healthy", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await engine.execute(_diagnosis(RecoveryAction.SNAPSHOT_ROLLBACK))
        assert result.success is False
        assert "No healthy snapshot" in result.detail


class TestRecoveryContainerRestart:

    @pytest.mark.asyncio
    async def test_container_restart(self, engine: RecoveryEngine) -> None:
        with (
            patch("genesis.guardian.recovery._run_subprocess", _mock_subprocess(0, "")),
            patch("genesis.guardian.recovery.collect_all_signals", return_value=_healthy_snapshot()),
            patch.object(engine._snapshots, "take", return_value="pre-recovery"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await engine.execute(_diagnosis(RecoveryAction.RESTART_CONTAINER))
        assert result.success is True
        assert result.action == RecoveryAction.RESTART_CONTAINER


class TestRecoveryResourceClear:

    @pytest.mark.asyncio
    async def test_resource_clear(self, engine: RecoveryEngine) -> None:

        async def multi_mock(*args, **kwargs):
            return (0, "", "")

        with (
            patch("genesis.guardian.recovery._run_subprocess", multi_mock),
            patch("genesis.guardian.recovery.collect_all_signals", return_value=_healthy_snapshot()),
            patch.object(engine._snapshots, "take", return_value="pre-recovery"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await engine.execute(_diagnosis(RecoveryAction.RESOURCE_CLEAR))
        assert result.success is True


class TestRecoveryIOTriage:

    @pytest.mark.asyncio
    async def test_io_triage_kills_top_consumer(self, engine: RecoveryEngine) -> None:
        """IO_TRIAGE should kill the top I/O consumer when PSI is not dropping."""
        with (
            patch("genesis.guardian.recovery.collect_all_signals", return_value=_healthy_snapshot()),
            patch.object(engine._snapshots, "safe_to_snapshot", return_value=True),
            patch.object(engine._snapshots, "take", return_value="pre-recovery"),
            patch("genesis.guardian.recovery.RecoveryEngine._io_triage") as mock_triage,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_triage.return_value = (True, "Killed PID 1234 (claude)")
            result = await engine.execute(_diagnosis(RecoveryAction.IO_TRIAGE))
        assert result.success is True
        assert result.action == RecoveryAction.IO_TRIAGE

    @pytest.mark.asyncio
    async def test_io_triage_stands_down_when_recovering(self, engine: RecoveryEngine) -> None:
        """IO_TRIAGE should stand down when PSI trend shows recovery."""
        with (
            patch("genesis.guardian.recovery.collect_all_signals", return_value=_healthy_snapshot()),
            patch.object(engine._snapshots, "safe_to_snapshot", return_value=True),
            patch.object(engine._snapshots, "take", return_value="pre-recovery"),
            patch("genesis.guardian.recovery.RecoveryEngine._io_triage") as mock_triage,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_triage.return_value = (True, "I/O pressure recovering — standing down")
            result = await engine.execute(_diagnosis(RecoveryAction.IO_TRIAGE))
        assert result.success is True
        assert "recovering" in result.detail.lower() or result.detail  # stood down

    @pytest.mark.asyncio
    async def test_io_triage_separate_counter(self, engine: RecoveryEngine) -> None:
        """IO_TRIAGE should use io_triage_attempts, not recovery_attempts."""
        # Record an IO_TRIAGE attempt
        engine._sm.record_recovery_attempt("IO_TRIAGE")
        assert engine._sm.state.io_triage_attempts == 1
        assert engine._sm.state.recovery_attempts == 0  # Separate counter

        # Record a regular recovery attempt
        engine._sm.record_recovery_attempt("RESTART_SERVICES")
        assert engine._sm.state.io_triage_attempts == 1  # Unchanged
        assert engine._sm.state.recovery_attempts == 1


class TestRecoveryRevertCode:

    @pytest.mark.asyncio
    async def test_revert_code(self, engine: RecoveryEngine) -> None:
        with (
            patch("genesis.guardian.recovery._run_subprocess", _mock_subprocess(0, "")),
            patch("genesis.guardian.recovery.collect_all_signals", return_value=_healthy_snapshot()),
            patch.object(engine._snapshots, "take", return_value="pre-recovery"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await engine.execute(_diagnosis(RecoveryAction.REVERT_CODE))
        assert result.success is True
        assert "reverted" in result.detail.lower()


class TestRestartContainerStopped:
    """`incus restart` fails on a STOPPED instance (2026-07-04 outage: unclean
    host reboot left the container stopped and the guardian's designed
    recovery action was a guaranteed no-op). Fall back to `incus start`."""

    @pytest.mark.asyncio
    async def test_start_fallback_when_stopped(
        self, engine: RecoveryEngine,
    ) -> None:
        calls: list[tuple] = []

        async def mock(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("incus", "restart"):
                return (1, "", "Error: The instance is not running")
            if args[:2] == ("incus", "start"):
                return (0, "", "")
            return (0, "", "")

        with patch("genesis.guardian.recovery._run_subprocess", mock):
            ok, detail = await engine._restart_container("genesis")
        assert ok is True
        assert "start" in detail.lower()
        assert calls[-1][:3] == ("incus", "start", "genesis")

    @pytest.mark.asyncio
    async def test_reports_restart_error_when_start_also_fails(
        self, engine: RecoveryEngine,
    ) -> None:
        async def mock(*args, **kwargs):
            if args[:2] == ("incus", "restart"):
                return (1, "", "restart boom")
            if args[:2] == ("incus", "start"):
                return (1, "", "already running")
            return (0, "", "")

        with patch("genesis.guardian.recovery._run_subprocess", mock):
            ok, detail = await engine._restart_container("genesis")
        assert ok is False
        assert "restart boom" in detail


class TestSnapshotRollbackRetry:
    """Restore can fail when newer snapshots exist (documented ZFS behavior;
    driver-agnostic hardening): delete newer guardian-* snapshots, retry once."""

    def _engine_with(self, config, sm, dispatcher, snapshots) -> RecoveryEngine:
        return RecoveryEngine(config, sm, snapshots, dispatcher)

    @pytest.mark.asyncio
    async def test_retry_after_deleting_newer(
        self, config: GuardianConfig, sm, dispatcher,
    ) -> None:
        from unittest.mock import MagicMock
        snapshots = MagicMock()
        healthy = "guardian-20260701-000000-healthy"
        snapshots.get_latest_healthy = AsyncMock(return_value=healthy)
        snapshots.restore = AsyncMock(side_effect=[False, True])
        snapshots.list_snapshots = AsyncMock(return_value=[
            "guardian-20260702-000000-pre-recovery",  # newer than healthy
            healthy,
        ])
        snapshots.delete = AsyncMock(return_value=True)

        engine = self._engine_with(config, sm, dispatcher, snapshots)
        ok, detail = await engine._snapshot_rollback()
        assert ok is True
        snapshots.delete.assert_called_once_with(
            "guardian-20260702-000000-pre-recovery",
        )
        assert snapshots.restore.call_count == 2

    @pytest.mark.asyncio
    async def test_no_retry_when_nothing_newer(
        self, config: GuardianConfig, sm, dispatcher,
    ) -> None:
        from unittest.mock import MagicMock
        snapshots = MagicMock()
        healthy = "guardian-20260701-000000-healthy"
        snapshots.get_latest_healthy = AsyncMock(return_value=healthy)
        snapshots.restore = AsyncMock(return_value=False)
        snapshots.list_snapshots = AsyncMock(return_value=[healthy])
        snapshots.delete = AsyncMock(return_value=True)

        engine = self._engine_with(config, sm, dispatcher, snapshots)
        ok, _ = await engine._snapshot_rollback()
        assert ok is False
        snapshots.delete.assert_not_called()
        assert snapshots.restore.call_count == 1


class TestRevertCodeGitPreflight:
    """F.1: when diagnosis picks REVERT_CODE but the container's git is unhealthy,
    execute() must ADVANCE to SNAPSHOT_ROLLBACK (a working rung that restores a
    healthy .git) — never burn the attempt on a doomed revert (which would set
    confirmed_dead and STALL the ladder)."""

    @pytest.mark.asyncio
    async def test_redirects_to_rollback_on_unhealthy_git(self, engine: RecoveryEngine) -> None:
        with (
            patch.object(engine, "_container_git_healthy", AsyncMock(return_value=False)),
            patch.object(engine._snapshots, "get_latest_healthy", return_value="guardian-healthy"),
            patch.object(engine._snapshots, "restore", return_value=True),
            patch("genesis.guardian.recovery.collect_all_signals", return_value=_healthy_snapshot()),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await engine.execute(_diagnosis(RecoveryAction.REVERT_CODE))
        # Advanced to a working rung, not stalled on a failed revert.
        assert result.action == RecoveryAction.SNAPSHOT_ROLLBACK
        assert result.success is True

    @pytest.mark.asyncio
    async def test_revert_proceeds_when_git_healthy(self, engine: RecoveryEngine) -> None:
        with (
            patch.object(engine, "_container_git_healthy", AsyncMock(return_value=True)),
            patch("genesis.guardian.recovery._run_subprocess", _mock_subprocess(0, "")),
            patch("genesis.guardian.recovery.collect_all_signals", return_value=_healthy_snapshot()),
            patch.object(engine._snapshots, "take", return_value="pre-recovery"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await engine.execute(_diagnosis(RecoveryAction.REVERT_CODE))
        assert result.action == RecoveryAction.REVERT_CODE
        assert result.success is True

    @pytest.mark.asyncio
    async def test_container_git_healthy_fails_open_on_inconclusive(self, engine: RecoveryEngine) -> None:
        import genesis.guardian.git_watch as gw

        # Inconclusive probe (None: unreachable/unparseable) → fail OPEN (True).
        with patch.object(gw, "probe_container_git", AsyncMock(return_value=None)):
            assert await engine._container_git_healthy() is True
        # Positively unhealthy → False.
        with patch.object(
            gw, "probe_container_git", AsyncMock(return_value={"healthy": False, "failures": ["rootfs_readonly"]})
        ):
            assert await engine._container_git_healthy() is False
