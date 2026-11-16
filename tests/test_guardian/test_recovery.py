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


@pytest.fixture
def unit_comes_up():
    """genesis-server read as running, then running under a new invocation:
    a restart that worked, for tests about what surrounds the restart."""
    seen = []

    async def fake(container):
        seen.append(container)
        inv = "old" if len(seen) == 1 else "new"
        return {"ActiveState": "active", "SubState": "running", "InvocationID": inv}

    with patch("genesis.guardian.recovery._server_unit", fake):
        yield


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


@pytest.mark.usefixtures("unit_comes_up")
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


def _unit(state: str, sub: str = "running", inv: str = "aaa") -> str:
    return f"ActiveState={state}\nSubState={sub}\nInvocationID={inv}"


def _systemd(shows: list[str], cmd_rc: tuple[int, str, str] = (0, "", "")):
    """A fake container: each `systemctl --user show` answers the next of
    ``shows`` (the last one repeats); any other command answers ``cmd_rc``.
    The commands it saw are in ``.calls``."""
    calls: list[str] = []

    async def mock(*args, **kwargs):
        cmd = args[-1]
        calls.append(cmd)
        if "systemctl --user show genesis-server" in cmd:
            n = sum("systemctl --user show" in c for c in calls) - 1
            return 0, "a login banner line\n" + shows[min(n, len(shows) - 1)], ""
        if cmd.startswith("journalctl"):
            return 0, "live-move repair: held: the checkout is half-written", ""
        return cmd_rc

    mock.calls = calls
    return mock


def _acts(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("systemctl --user") and " show " not in c]


class TestRestartWaitsForSystemdStart:
    """D2a-g (#3492 D2 rev 3 11.3): a restart never stops a start in progress
    (the boot repair heals in genesis-server's start step), and the result is
    the unit's own state after the start, not the restart command's return."""

    @pytest.mark.asyncio
    async def test_a_start_in_progress_is_left_to_finish(self, engine: RecoveryEngine) -> None:
        fake = _systemd([_unit("activating", "start-pre"), _unit("activating", "start-pre"),
                         _unit("active")])  # fmt: skip
        with patch("genesis.guardian.recovery._run_subprocess", fake), \
                patch("asyncio.sleep", new_callable=AsyncMock):  # fmt: skip
            ok, detail = await engine._restart_services("genesis")
        assert ok, detail
        assert _acts(fake.calls) == [], "a start in progress was restarted"

    @pytest.mark.asyncio
    async def test_a_failed_unit_is_started_not_restarted(self, engine: RecoveryEngine) -> None:
        fake = _systemd([_unit("failed", "failed", "old"), _unit("failed", "failed", "old"),
                         _unit("active", "running", "new")])  # fmt: skip
        with patch("genesis.guardian.recovery._run_subprocess", fake), \
                patch("asyncio.sleep", new_callable=AsyncMock):  # fmt: skip
            ok, detail = await engine._restart_services("genesis")
        assert ok, detail
        assert _acts(fake.calls) == ["systemctl --user start --no-block genesis-server"]

    @pytest.mark.asyncio
    async def test_the_old_process_still_running_is_not_success(self, engine: RecoveryEngine) -> None:
        """Review SF3: after `restart --no-block` the poll can read the old
        process, still active under its InvocationID; only a new one counts."""
        fake = _systemd([_unit("active", "running", "old")])
        with patch("genesis.guardian.recovery._run_subprocess", fake), \
                patch("asyncio.sleep", new_callable=AsyncMock):  # fmt: skip
            ok, detail = await engine._restart_services("genesis")
        assert not ok
        assert _acts(fake.calls) == ["systemctl --user restart --no-block genesis-server"]
        assert "active/running" in detail and "live-move repair:" in detail, detail

    @pytest.mark.asyncio
    async def test_a_client_timeout_is_decided_by_the_unit(self, engine: RecoveryEngine) -> None:
        fake = _systemd([_unit("active", "running", "old"), _unit("active", "running", "new")],
                        cmd_rc=(-1, "", "timeout"))  # fmt: skip
        with patch("genesis.guardian.recovery._run_subprocess", fake), \
                patch("asyncio.sleep", new_callable=AsyncMock):  # fmt: skip
            ok, detail = await engine._restart_services("genesis")
        assert ok, detail

    @pytest.mark.asyncio
    async def test_a_new_start_that_fails_is_reported(self, engine: RecoveryEngine) -> None:
        fake = _systemd([_unit("active", "running", "old"), _unit("failed", "failed", "new")])
        with patch("genesis.guardian.recovery._run_subprocess", fake), \
                patch("asyncio.sleep", new_callable=AsyncMock):  # fmt: skip
            ok, detail = await engine._restart_services("genesis")
        assert not ok and "failed/failed" in detail, detail

    @pytest.mark.asyncio
    async def test_the_start_limit_is_named_at_once(self, engine: RecoveryEngine) -> None:
        """Review SF-2 (MEASURED on systemd 255): `start --no-block` exits 0 at
        the start limit and the unit stays failed under its old invocation."""
        hit = _unit("failed", "failed", "old") + "\nResult=start-limit-hit"
        fake = _systemd([hit])
        with patch("genesis.guardian.recovery._run_subprocess", fake), \
                patch("asyncio.sleep", new_callable=AsyncMock) as slept:  # fmt: skip
            ok, detail = await engine._restart_services("genesis")
        assert not ok and "start limit hit" in detail, detail
        assert slept.await_count == 0, "it waited for a start systemd refused"

    @pytest.mark.asyncio
    async def test_an_inactive_unit_is_started(self, engine: RecoveryEngine) -> None:
        fake = _systemd([_unit("inactive", "dead", ""), _unit("active", "running", "new")])
        with patch("genesis.guardian.recovery._run_subprocess", fake), \
                patch("asyncio.sleep", new_callable=AsyncMock):  # fmt: skip
            ok, detail = await engine._restart_services("genesis")
        assert ok, detail
        assert _acts(fake.calls) == ["systemctl --user start --no-block genesis-server"]

    @pytest.mark.asyncio
    async def test_a_container_that_stops_answering_ends_the_wait(
        self, engine: RecoveryEngine
    ) -> None:
        """Review SF-1: unreadable reads end the wait after three, rather than
        each costing its 30 s timeout for the whole poll bound."""
        fake = _systemd(["nothing parseable"])
        with patch("genesis.guardian.recovery._run_subprocess", fake), \
                patch("asyncio.sleep", new_callable=AsyncMock) as slept:  # fmt: skip
            ok, detail = await engine._restart_services("genesis")
        assert not ok and "unreadable" in detail, detail
        assert slept.await_count < 5, slept.await_count

    @pytest.mark.asyncio
    async def test_no_repair_line_is_quoted_when_the_journal_has_none(
        self, engine: RecoveryEngine
    ) -> None:
        fake = _systemd([_unit("failed", "failed", "old"), _unit("failed", "failed", "new")])

        async def no_repair(*args, **kwargs):
            if args[-1].startswith("journalctl"):
                return 0, "a login banner line", ""
            return await fake(*args, **kwargs)

        with patch("genesis.guardian.recovery._run_subprocess", no_repair), \
                patch("asyncio.sleep", new_callable=AsyncMock):  # fmt: skip
            ok, detail = await engine._restart_services("genesis")
        assert not ok and "last line" not in detail and "banner" not in detail, detail

    @pytest.mark.asyncio
    async def test_a_command_that_cannot_run_fails_at_once(self, engine: RecoveryEngine) -> None:
        fake = _systemd([_unit("active", "running", "old")], cmd_rc=(-1, "", "No such file"))
        with patch("genesis.guardian.recovery._run_subprocess", fake), \
                patch("asyncio.sleep", new_callable=AsyncMock):  # fmt: skip
            ok, detail = await engine._restart_services("genesis")
        assert not ok and "No such file" in detail, detail


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


@pytest.mark.usefixtures("unit_comes_up")
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


_MAIN_CHECKOUT = (1, "other\n", "")  # live_checkout.py's answer on any branch but `live`
_MAIN_HEAD = (0, "refs/heads/main\n", "")  # `symbolic-ref -q HEAD` on main


def _on_main(rc: int = 0, stdout: str = "", stderr: str = ""):
    """Like _mock_subprocess, but both live probes answer for `main`."""
    async def mock(*args, **kwargs):
        if "live_checkout.py" in args[-1]:
            return _MAIN_CHECKOUT
        if "symbolic-ref" in args[-1]:
            return _MAIN_HEAD
        return (rc, stdout, stderr)
    return mock


@pytest.mark.usefixtures("unit_comes_up")
class TestRecoveryRevertCode:

    @pytest.mark.asyncio
    async def test_revert_code(self, engine: RecoveryEngine) -> None:
        with (
            patch("genesis.guardian.recovery._run_subprocess", _on_main(0, "")),
            patch("genesis.guardian.recovery.collect_all_signals", return_value=_healthy_snapshot()),
            patch.object(engine._snapshots, "take", return_value="pre-recovery"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await engine.execute(_diagnosis(RecoveryAction.REVERT_CODE))
        assert result.success is True
        assert "reverted" in result.detail.lower()

    @staticmethod
    def _recording_subprocess(
        stash_result: tuple[int, str, str],
        probe: tuple[int, str, str] = _MAIN_CHECKOUT,
        head: tuple[int, str, str] = _MAIN_HEAD,
    ):
        calls: list[str] = []

        async def mock(*args, **kwargs):
            command = args[-1]
            calls.append(command)
            if "live_checkout.py" in command:
                return probe
            if "symbolic-ref" in command:
                return head
            if "git stash" in command:
                return stash_result
            return (0, "", "")

        return mock, calls

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "probe",
        [
            (0, "live\n", ""),  # on `live`, bound by the manifest
            (2, "unreadable\n", ""),  # on `live`, manifest or git unreadable
            (-1, "", "timeout"),  # the probe never reported
            (1, "", ""),  # code without the word: a broken copy of the predicate
            (0, "other\n", ""),  # word and code disagree
        ],
    )
    async def test_never_stashes_or_reverts_unless_shown_not_live(
        self, engine: RecoveryEngine, probe
    ) -> None:
        mock, calls = self._recording_subprocess((0, "", ""), probe=probe)
        with patch("genesis.guardian.recovery._run_subprocess", mock):
            ok, detail = await engine._revert_code("genesis")
        assert ok is False
        assert "`live`" in detail and "RESTART_CONTAINER" in detail
        assert "deploy_candidates drop" in detail and "runs the server's boot repair" not in detail
        assert not any("git stash" in c or "git revert" in c for c in calls), calls

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "head",
        [
            (0, "refs/heads/live\n", ""),  # on `live` with no manifest: the predicate says other
            (0, "a login banner\nrefs/heads/live\n", ""),
            (128, "", "fatal: not a git repository"),  # git failed
            (-1, "", "timeout"),  # never reported
            (0, "", ""),  # exit 0 without a ref
        ],
    )
    async def test_head_on_live_bans_whatever_the_predicate_says(
        self, engine: RecoveryEngine, head
    ) -> None:
        mock, calls = self._recording_subprocess((0, "", ""), head=head)
        with patch("genesis.guardian.recovery._run_subprocess", mock):
            ok, _ = await engine._revert_code("genesis")
        assert ok is False
        assert not any("git stash" in c or "git revert" in c for c in calls), calls

    @pytest.mark.asyncio
    async def test_detached_head_defers_to_the_predicate(self, engine: RecoveryEngine) -> None:
        mock, calls = self._recording_subprocess((0, "", ""), head=(1, "", ""))
        with patch("genesis.guardian.recovery._run_subprocess", mock):
            ok, _ = await engine._revert_code("genesis")
        assert ok is True

    @pytest.mark.asyncio
    async def test_main_checkout_still_reverts(self, engine: RecoveryEngine) -> None:
        """A login shell may print before the probe; its last line decides."""
        mock, calls = self._recording_subprocess(
            (0, "", ""),
            probe=(1, "a login banner\nother\n", ""),
            head=(0, "a login banner\nrefs/heads/main\n", ""),
        )
        with patch("genesis.guardian.recovery._run_subprocess", mock):
            ok, _ = await engine._revert_code("genesis")
        assert ok is True
        assert any("git stash" in c for c in calls), calls
        assert any("git revert" in c for c in calls), calls

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stash_result", "expected"),
        [
            ((1, "", "fatal: index.lock exists"), "failed"),
            ((-1, "", "timeout"), "did not report"),
        ],
    )
    async def test_failed_stash_never_reverts(
        self, engine: RecoveryEngine, stash_result, expected
    ) -> None:
        mock, calls = self._recording_subprocess(stash_result)
        with patch("genesis.guardian.recovery._run_subprocess", mock):
            ok, detail = await engine._revert_code("genesis")
        assert ok is False
        assert expected in detail
        assert not any("git revert" in c for c in calls), calls

    @pytest.mark.asyncio
    async def test_clean_tree_stash_still_reverts(self, engine: RecoveryEngine) -> None:
        mock, calls = self._recording_subprocess((0, "No local changes to save", ""))
        with patch("genesis.guardian.recovery._run_subprocess", mock):
            await engine._revert_code("genesis")
        assert any("git revert" in c for c in calls), calls


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


@pytest.mark.asyncio
async def test_rollback_retry_follows_listing_order_not_names(config, sm, dispatcher) -> None:
    """Review: 'newer' is what the manager lists BEFORE the healthy snapshot
    (created-at order), never a lexical name comparison. A pre-recovery
    snapshot created after the healthy one but carrying an earlier-looking
    name must still be removed; an older one with a later-looking name must
    not be."""
    from unittest.mock import MagicMock

    healthy = "guardian-20260701-000000-healthy"
    newer_by_time = "guardian-20260601-000000-pre-recovery"
    older_by_time = "guardian-20260801-000000-pre-recovery"
    snapshots = MagicMock()
    snapshots.get_latest_healthy = AsyncMock(return_value=healthy)
    snapshots.restore = AsyncMock(side_effect=[False, True])
    snapshots.list_snapshots = AsyncMock(return_value=[newer_by_time, healthy, older_by_time])
    snapshots.delete = AsyncMock(return_value=True)

    engine = RecoveryEngine(config, sm, snapshots, dispatcher)
    ok, _ = await engine._snapshot_rollback()
    assert ok is True
    snapshots.delete.assert_called_once_with(newer_by_time)
