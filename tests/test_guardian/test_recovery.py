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


_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _revert_mock(
    *, head: tuple[int, str] = (0, _SHA_A), stash: tuple[int, str] = (0, ""),
    revert: tuple[int, str] = (0, ""), abort: tuple[int, str] = (0, ""),
    calls: list[str] | None = None,
):
    """Mock `_run_subprocess` for the revert path, dispatching on the shell command.

    ``head`` is the (rc, stdout) of the `git rev-parse HEAD` probe; ``stash``,
    ``revert`` and ``abort`` are the (rc, stderr) of their respective calls.
    Order matters: `--abort` is checked before the bare revert, since its
    command string also contains "git revert".
    """
    async def mock(*args, **kwargs):
        cmd = args[-1]
        if calls is not None:
            calls.append(cmd)
        if "rev-parse" in cmd:
            return (head[0], head[1], "")
        if "git stash" in cmd:
            return (stash[0], "", stash[1])
        if "revert --abort" in cmd:
            return (abort[0], "", abort[1])
        if "git revert" in cmd:
            return (revert[0], "", revert[1])
        return (0, "", "")
    return mock


class TestRecoveryRevertCode:

    @pytest.mark.asyncio
    async def test_revert_code(self, engine: RecoveryEngine) -> None:
        with (
            patch("genesis.guardian.recovery._run_subprocess", _revert_mock()),
            patch("genesis.guardian.recovery.collect_all_signals", return_value=_healthy_snapshot()),
            patch.object(engine._snapshots, "take", return_value="pre-recovery"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await engine.execute(_diagnosis(RecoveryAction.REVERT_CODE))
        assert result.success is True
        assert "reverted" in result.detail.lower()


class TestRevertCodeGuards:
    """REVERT_CODE runs `git stash` then `git revert` against the container's
    LIVE dev checkout. Two ways that destroyed work it should not touch:

    1. The stash's exit code was DISCARDED (`rc, _, _ = ...`), so a failed stash
       still went on to revert — on top of uncommitted work never saved.
    2. HEAD was resolved implicitly by the revert itself. With a stash round-trip
       in between, a deploy landing in that window meant reverting a commit the
       action never looked at.
    """

    @pytest.mark.asyncio
    async def test_failed_stash_aborts_before_reverting(
        self, engine: RecoveryEngine,
    ) -> None:
        calls: list[str] = []
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(stash=(1, "fatal: unable to write new index file"), calls=calls),
        ):
            ok, detail = await engine._revert_code("genesis")
        assert ok is False
        assert "stash" in detail.lower()
        assert not any("git revert" in c for c in calls), (
            "a failed stash must abort — reverting on top of unsaved work destroys it"
        )

    @pytest.mark.asyncio
    async def test_stash_timeout_aborts_before_reverting(
        self, engine: RecoveryEngine,
    ) -> None:
        # _run_subprocess returns (-1, "", "timeout") on timeout — rc != 0.
        calls: list[str] = []
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(stash=(-1, "timeout"), calls=calls),
        ):
            ok, detail = await engine._revert_code("genesis")
        assert ok is False
        assert not any("git revert" in c for c in calls)

    @pytest.mark.asyncio
    async def test_reverts_the_pinned_sha_not_symbolic_head(
        self, engine: RecoveryEngine,
    ) -> None:
        calls: list[str] = []
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(head=(0, _SHA_B), calls=calls),
        ):
            ok, _ = await engine._revert_code("genesis")
        assert ok is True
        revert_cmds = [c for c in calls if "git revert" in c]
        assert len(revert_cmds) == 1
        assert _SHA_B in revert_cmds[0], "the resolved sha must be what gets reverted"
        assert "HEAD" not in revert_cmds[0], (
            "reverting symbolic HEAD re-resolves it — a deploy landing after the "
            "probe would then revert a commit that was never vetted"
        )

    @pytest.mark.asyncio
    async def test_refuses_when_head_cannot_be_resolved(
        self, engine: RecoveryEngine,
    ) -> None:
        calls: list[str] = []
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(head=(128, ""), calls=calls),
        ):
            ok, detail = await engine._revert_code("genesis")
        assert ok is False
        assert "resolve" in detail.lower()
        assert not any("git stash" in c for c in calls), (
            "refuse BEFORE stashing — a refusal that stashes still displaces the work"
        )
        assert not any("git revert" in c for c in calls)

    @pytest.mark.parametrize(
        "contaminated",
        [
            "nvm: loaded\n" + _SHA_A,          # login-shell banner ahead of the value
            "$(rm -rf /)",                      # not sha-shaped at all
            _SHA_A + "; rm -rf /",              # sha-shaped prefix, shell suffix
            "HEAD",
            "",
        ],
    )
    @pytest.mark.asyncio
    async def test_refuses_any_head_value_that_is_not_a_bare_sha(
        self, engine: RecoveryEngine, contaminated: str,
    ) -> None:
        # The sha is interpolated into a shell command inside the container, so
        # the shape check is a boundary, not a formality. `| tail -n1` strips a
        # banner; this refuses whatever still is not a bare 40-hex sha.
        calls: list[str] = []
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(head=(0, contaminated), calls=calls),
        ):
            ok, _ = await engine._revert_code("genesis")
        assert ok is False
        assert not any("git revert" in c for c in calls)

    @pytest.mark.asyncio
    async def test_failed_revert_aborts_so_the_tree_is_not_left_mid_revert(
        self, engine: RecoveryEngine,
    ) -> None:
        # Without the abort the two guards deadlock: a conflicted revert leaves
        # an unmerged index, `git stash` refuses an unmerged index, and the stash
        # guard then refuses this rung on EVERY later attempt.
        calls: list[str] = []
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(revert=(1, "error: could not revert... conflict"), calls=calls),
        ):
            ok, detail = await engine._revert_code("genesis")
        assert ok is False
        assert "revert failed" in detail.lower()
        assert any("revert --abort" in c for c in calls), (
            "a failed revert must be aborted, or the conflicted tree disables the rung"
        )

    @pytest.mark.asyncio
    async def test_abort_failure_is_reported_but_does_not_mask_the_revert_failure(
        self, engine: RecoveryEngine,
    ) -> None:
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(revert=(1, "conflict"), abort=(1, "no revert in progress")),
        ):
            ok, detail = await engine._revert_code("genesis")
        assert ok is False
        # The caller still learns why the RECOVERY failed, not why cleanup did.
        assert "revert failed" in detail.lower()

    @pytest.mark.asyncio
    async def test_nonzero_probe_rc_refuses_even_when_stdout_looks_valid(
        self, engine: RecoveryEngine,
    ) -> None:
        # Isolates the `rc != 0` half of the probe guard. Without pipefail the
        # pipe reports tail's status, so a failing `git rev-parse` reads as rc=0;
        # a test that also supplies empty stdout would pass on the regex alone
        # and leave this clause unverified.
        calls: list[str] = []
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(head=(128, _SHA_A), calls=calls),
        ):
            ok, _ = await engine._revert_code("genesis")
        assert ok is False
        assert not any("git stash" in c for c in calls)

    @pytest.mark.asyncio
    async def test_probe_command_restores_pipeline_exit_status(
        self, engine: RecoveryEngine,
    ) -> None:
        # The mock cannot execute a shell, so assert on the command TEXT: the
        # pipe must not be allowed to mask a failing `git rev-parse`.
        calls: list[str] = []
        with patch("genesis.guardian.recovery._run_subprocess", _revert_mock(calls=calls)):
            await engine._revert_code("genesis")
        probe = next(c for c in calls if "rev-parse" in c)
        assert "pipefail" in probe, (
            "`git rev-parse HEAD | tail -n1` reports tail's status; rev-parse "
            "exits 128 AND prints 'HEAD' on a broken repo, so the rc check is "
            "dead without pipefail"
        )

    @pytest.mark.parametrize("short_sha", ["a" * 7, "a" * 39, "a" * 41])
    @pytest.mark.asyncio
    async def test_refuses_a_sha_of_the_wrong_length(
        self, engine: RecoveryEngine, short_sha: str,
    ) -> None:
        # Isolates the regex's exact-40 requirement: an abbreviated sha is hex
        # and would satisfy a length-relaxed pattern, but is not what the probe
        # is contracted to return.
        calls: list[str] = []
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(head=(0, short_sha), calls=calls),
        ):
            ok, _ = await engine._revert_code("genesis")
        assert ok is False
        assert not any("git revert" in c for c in calls)

    @pytest.mark.asyncio
    async def test_success_detail_says_where_uncommitted_work_went(
        self, engine: RecoveryEngine,
    ) -> None:
        # The stash is never popped, so the outcome string is the only place the
        # recovery alert can tell the owner their work is recoverable.
        with patch("genesis.guardian.recovery._run_subprocess", _revert_mock()):
            ok, detail = await engine._revert_code("genesis")
        assert ok is True
        assert "stash" in detail.lower()


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
