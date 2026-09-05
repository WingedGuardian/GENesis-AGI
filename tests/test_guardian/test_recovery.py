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
_SHA_OTHER = "c" * 40


def _revert_mock(
    *, head: tuple[int, str] = (0, _SHA_A), in_flight: str = "",
    left_behind: str | None = None, stash: tuple[int, str] = (0, ""),
    revert: tuple[int, str] = (0, ""), abort: tuple[int, str] = (0, ""),
    calls: list[str] | None = None,
):
    """Mock `_run_subprocess` for the revert path, dispatching on the shell command.

    ``head``        (rc, stdout) of the `git rev-parse HEAD` probe. stdout may
                    carry a login-shell banner ahead of the sha.
    ``in_flight``   REVERT_HEAD BEFORE we start ("" = no revert in progress).
    ``left_behind`` REVERT_HEAD AFTER our revert ran; defaults to the sha we
                    pinned when the revert failed, "" when it succeeded.
    ``stash`` / ``revert`` / ``abort``  (rc, stderr) of those calls.

    Dispatch order matters: REVERT_HEAD is matched before the bare `rev-parse
    HEAD`, and `revert --abort` before the bare `git revert` — each pair shares
    a substring.
    """
    state = {"revert_ran": False}

    async def mock(*args, **kwargs):
        cmd = args[-1]
        if calls is not None:
            calls.append(cmd)
        if "REVERT_HEAD" in cmd:
            if not state["revert_ran"]:
                return (0, in_flight, "")
            if left_behind is not None:
                return (0, left_behind, "")
            return (0, (head[1] if revert[0] != 0 else ""), "")
        if "rev-parse HEAD" in cmd:
            return (head[0], head[1], "")
        if "git stash" in cmd:
            return (stash[0], "", stash[1])
        if "revert --abort" in cmd:
            return (abort[0], "", abort[1])
        if "git revert" in cmd:
            state["revert_ran"] = True
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
    LIVE dev checkout. Ways that destroyed work it should not touch:

    1. The stash's exit code was DISCARDED, so a failed stash still reverted on
       top of uncommitted work that was never saved.
    2. HEAD was resolved implicitly by the revert itself, so a commit landing
       during the stash round-trip redirected the revert onto a commit nothing
       had inspected.
    3. A failed revert left the tree mid-revert; `git stash` refuses an unmerged
       index, so the new stash guard would then decline this rung forever.
    4. Cleaning that up unconditionally would reset a revert ANOTHER session
       started, destroying a conflict resolution our stash never captured.
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
            ok, _ = await engine._revert_code("genesis")
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
        revert_cmds = [c for c in calls if "git revert" in c and "--abort" not in c]
        assert len(revert_cmds) == 1
        assert _SHA_B in revert_cmds[0], "the resolved sha must be what gets reverted"
        assert "HEAD" not in revert_cmds[0], (
            "reverting symbolic HEAD re-resolves it — a commit landing after the "
            "probe would then revert something that was never vetted"
        )

    @pytest.mark.asyncio
    async def test_login_shell_banner_does_not_disable_the_rung(
        self, engine: RecoveryEngine,
    ) -> None:
        # `su -` runs a LOGIN shell: its startup files write to the same stdout
        # BEFORE the -c command runs, so a pipe inside that command cannot filter
        # them — the banner was never its input. Parsing the last line in Python
        # is what stops a supported shell config failing this rung closed on
        # every attempt.
        banner = "Found '.nvmrc' with version <22>\ndirenv: loading ~/.envrc\n"
        calls: list[str] = []
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(head=(0, banner + _SHA_B), calls=calls),
        ):
            ok, _ = await engine._revert_code("genesis")
        assert ok is True, "a login-shell banner must not be read as a bad sha"
        revert_cmds = [c for c in calls if "git revert" in c and "--abort" not in c]
        assert _SHA_B in revert_cmds[0]

    @pytest.mark.asyncio
    async def test_probe_does_not_pipe_away_its_exit_status(
        self, engine: RecoveryEngine,
    ) -> None:
        # The mock cannot execute a shell, so assert on the command TEXT. A pipe
        # makes the probe report the filter's status instead of git's, and
        # `git rev-parse HEAD` exits 128 while printing the literal "HEAD".
        calls: list[str] = []
        with patch("genesis.guardian.recovery._run_subprocess", _revert_mock(calls=calls)):
            await engine._revert_code("genesis")
        probe = next(c for c in calls if "rev-parse HEAD" in c)
        assert "|" not in probe, (
            "piping the probe replaces git's exit status with the filter's; the "
            "last-line parse belongs in Python, where rc stays meaningful"
        )

    @pytest.mark.asyncio
    async def test_nonzero_probe_rc_refuses_even_when_stdout_looks_valid(
        self, engine: RecoveryEngine,
    ) -> None:
        # Isolates the `rc != 0` half of the probe guard: a test that ALSO passed
        # empty stdout would pass on the regex alone and leave this clause dead.
        calls: list[str] = []
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(head=(128, _SHA_A), calls=calls),
        ):
            ok, _ = await engine._revert_code("genesis")
        assert ok is False
        assert not any("git stash" in c for c in calls)

    @pytest.mark.parametrize("contaminated", ["$(id)", "`id`", _SHA_A + "; echo INJECTED", "HEAD", "", "a" * 7, "a" * 39, "a" * 41])
    @pytest.mark.asyncio
    async def test_refuses_any_head_value_that_is_not_a_bare_sha(
        self, engine: RecoveryEngine, contaminated: str,
    ) -> None:
        # The sha is interpolated into a shell command inside the container, so
        # the shape check is a boundary, not a formality. The length cases pin
        # the exact-40 requirement: an abbreviated sha is still hex.
        calls: list[str] = []
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(head=(0, contaminated), calls=calls),
        ):
            ok, _ = await engine._revert_code("genesis")
        assert ok is False
        assert not any("git revert" in c for c in calls)

    @pytest.mark.asyncio
    async def test_refuses_when_another_revert_is_already_in_progress(
        self, engine: RecoveryEngine,
    ) -> None:
        calls: list[str] = []
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(in_flight=_SHA_OTHER, calls=calls),
        ):
            ok, detail = await engine._revert_code("genesis")
        assert ok is False
        assert "already in progress" in detail.lower()
        assert not any("git stash" in c for c in calls), (
            "refuse BEFORE stashing — our own revert would fail against another "
            "session's sequencer state, and cleanup would then abort THEIRS"
        )

    @pytest.mark.asyncio
    async def test_failed_revert_aborts_only_our_own(
        self, engine: RecoveryEngine,
    ) -> None:
        calls: list[str] = []
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(revert=(1, "conflict"), calls=calls),
        ):
            ok, detail = await engine._revert_code("genesis")
        assert ok is False
        assert "revert failed" in detail.lower()
        assert any("revert --abort" in c for c in calls), (
            "our own failed revert must be aborted, or the conflicted tree "
            "disables this rung permanently"
        )

    @pytest.mark.asyncio
    async def test_does_not_abort_a_revert_this_did_not_start(
        self, engine: RecoveryEngine,
    ) -> None:
        # Race window: another session began a revert between our pre-check and
        # our own revert. Aborting it would reset their state and destroy a
        # staged conflict resolution our stash never captured.
        calls: list[str] = []
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(revert=(1, "conflict"), left_behind=_SHA_OTHER, calls=calls),
        ):
            ok, detail = await engine._revert_code("genesis")
        assert ok is False
        assert not any("revert --abort" in c for c in calls)
        assert "did not start" in detail.lower()

    @pytest.mark.asyncio
    async def test_no_abort_when_the_failed_revert_left_nothing_behind(
        self, engine: RecoveryEngine,
    ) -> None:
        calls: list[str] = []
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(revert=(1, "bad object"), left_behind="", calls=calls),
        ):
            ok, detail = await engine._revert_code("genesis")
        assert ok is False
        assert not any("revert --abort" in c for c in calls)
        assert "revert failed" in detail.lower()

    @pytest.mark.asyncio
    async def test_abort_failure_is_surfaced_in_the_result_not_only_logged(
        self, engine: RecoveryEngine,
    ) -> None:
        # The recovery ALERT is built from this string. If cleanup failed, the
        # checkout is left with an unmerged index that makes every later stash
        # fail — reporting only the original revert error hides that.
        with patch(
            "genesis.guardian.recovery._run_subprocess",
            _revert_mock(revert=(1, "conflict"), abort=(1, "no revert in progress")),
        ):
            ok, detail = await engine._revert_code("genesis")
        assert ok is False
        assert "revert failed" in detail.lower(), "the original cause must survive"
        assert "manual repair" in detail.lower(), "the poisoned checkout must be named"

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
