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
