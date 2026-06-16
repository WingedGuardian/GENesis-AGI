"""Tests for Guardian main check logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.guardian.check import (
    _build_dispatcher,
    _handle_cc_resolved,
    _handle_confirmed_dead,
    _handle_healthy,
    _write_guardian_heartbeat,
    run_check,
)
from genesis.guardian.config import GuardianConfig
from genesis.guardian.diagnosis import DiagnosisEngine, DiagnosisResult, RecoveryAction
from genesis.guardian.health_signals import HealthSnapshot, PauseState, SignalResult
from genesis.guardian.state_machine import ConfirmationStateMachine, GuardianState


@pytest.fixture
def config(tmp_path: Path) -> GuardianConfig:
    cfg = GuardianConfig()
    cfg.state_dir = str(tmp_path / "guardian-state")
    return cfg


def _healthy_snapshot() -> HealthSnapshot:
    return HealthSnapshot(
        signals={
            name: SignalResult(name, True, 1.0, "ok", "t")
            for name in [
                "container_exists", "icmp_reachable", "health_api",
                "heartbeat_canary", "log_freshness",
            ]
        },
        pause_state=PauseState(paused=False),
    )


class TestBuildDispatcher:

    def test_no_credentials(self) -> None:
        config = GuardianConfig()
        dispatcher = _build_dispatcher(config)
        assert len(dispatcher._channels) == 0

    def test_with_credentials(self) -> None:
        config = GuardianConfig()
        config.alert.telegram_bot_token = "test-token"
        config.alert.telegram_chat_id = "12345"
        dispatcher = _build_dispatcher(config)
        assert len(dispatcher._channels) == 1


class TestWriteGuardianHeartbeat:

    @pytest.mark.asyncio
    async def test_writes_heartbeat(self) -> None:
        config = GuardianConfig()
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0
        mock_proc.pid = 9999

        with patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ) as mock_create:
            await _write_guardian_heartbeat(config)
        mock_create.assert_called_once()
        # Verify stdin was provided with heartbeat data
        call_kwargs = mock_create.call_args
        assert call_kwargs.kwargs.get("stdin") is not None


class TestHandleHealthy:

    @pytest.mark.asyncio
    async def test_does_not_write_heartbeat_directly(self, config: GuardianConfig) -> None:
        """_handle_healthy must NOT call the heartbeat — run_check owns it."""
        snapshots = MagicMock()
        snapshots.prune = AsyncMock(return_value=0)

        with patch(
            "genesis.guardian.check._write_guardian_heartbeat",
            AsyncMock(),
        ) as mock_hb:
            await _handle_healthy(config, snapshots)
        mock_hb.assert_not_called()

    @pytest.mark.asyncio
    async def test_prunes_when_due(self, config: GuardianConfig) -> None:
        snapshots = MagicMock()
        snapshots.prune = AsyncMock(return_value=2)

        await _handle_healthy(config, snapshots)
        # Should attempt prune (no marker file exists = overdue)
        snapshots.prune.assert_called_once()


class TestRunCheck:

    @pytest.mark.asyncio
    async def test_healthy_cycle(self, config: GuardianConfig) -> None:
        """Full healthy cycle: collect signals → HEALTHY → write heartbeat → save state."""
        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_healthy_snapshot()),
            ),
            patch("genesis.guardian.check._write_guardian_heartbeat", AsyncMock()),
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)

        # State file should exist after run
        state_file = config.state_path / "state.json"
        assert state_file.exists()

    @pytest.mark.asyncio
    async def test_writes_heartbeat_on_healthy(self, config: GuardianConfig) -> None:
        """Successful HEALTHY check cycle writes heartbeat once."""
        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_healthy_snapshot()),
            ),
            patch(
                "genesis.guardian.check._write_guardian_heartbeat", AsyncMock(),
            ) as mock_hb,
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)
        mock_hb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_writes_heartbeat_on_signal_dropped(self, config: GuardianConfig) -> None:
        """SIGNAL_DROPPED is still a successful cycle — heartbeat fires."""
        dead_snapshot = HealthSnapshot(
            signals={
                "container_exists": SignalResult("container_exists", False, 1.0, "down", "t"),
                "icmp_reachable": SignalResult("icmp_reachable", False, 1.0, "down", "t"),
                "health_api": SignalResult("health_api", True, 1.0, "ok", "t"),
                "heartbeat_canary": SignalResult("heartbeat_canary", True, 1.0, "ok", "t"),
                "log_freshness": SignalResult("log_freshness", True, 1.0, "ok", "t"),
            },
            pause_state=PauseState(paused=False),
        )
        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=dead_snapshot),
            ),
            patch(
                "genesis.guardian.check._write_guardian_heartbeat", AsyncMock(),
            ) as mock_hb,
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)
        mock_hb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_write_heartbeat_on_crash(self, config: GuardianConfig) -> None:
        """If _check_cycle raises, heartbeat must NOT fire — Guardian failure
        should be visible to Genesis-side monitoring."""
        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch(
                "genesis.guardian.check._write_guardian_heartbeat", AsyncMock(),
            ) as mock_hb,
            patch("genesis.guardian.check.load_secrets", return_value={}),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await run_check(config)
        mock_hb.assert_not_called()

    @pytest.mark.asyncio
    async def test_state_persists_across_runs(self, config: GuardianConfig) -> None:
        """State should persist between invocations."""
        dead_snapshot = HealthSnapshot(
            signals={
                "container_exists": SignalResult("container_exists", False, 1.0, "down", "t"),
                "icmp_reachable": SignalResult("icmp_reachable", False, 1.0, "down", "t"),
                "health_api": SignalResult("health_api", True, 1.0, "ok", "t"),
                "heartbeat_canary": SignalResult("heartbeat_canary", True, 1.0, "ok", "t"),
                "log_freshness": SignalResult("log_freshness", True, 1.0, "ok", "t"),
            },
            pause_state=PauseState(paused=False),
        )

        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=dead_snapshot),
            ),
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)

        # First run should move to SIGNAL_DROPPED
        import json
        state = json.loads((config.state_path / "state.json").read_text())
        assert state["current_state"] == "signal_dropped"

    @pytest.mark.asyncio
    async def test_saves_state_on_error(self, config: GuardianConfig) -> None:
        """State should be saved even if check_cycle raises."""
        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch("genesis.guardian.check.load_secrets", return_value={}),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await run_check(config)

        # State file should still be written
        state_file = config.state_path / "state.json"
        assert state_file.exists()


def _dead_snapshot() -> HealthSnapshot:
    """Container/probe failure — not all_alive."""
    return HealthSnapshot(
        signals={
            "container_exists": SignalResult("container_exists", False, 1.0, "down", "t"),
            "icmp_reachable": SignalResult("icmp_reachable", False, 1.0, "down", "t"),
            "health_api": SignalResult("health_api", False, 1.0, "down", "t"),
            "heartbeat_canary": SignalResult("heartbeat_canary", True, 1.0, "ok", "t"),
            "log_freshness": SignalResult("log_freshness", True, 1.0, "ok", "t"),
        },
        pause_state=PauseState(paused=False),
    )


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _cc_unavailable_diagnosis() -> DiagnosisResult:
    """The escalate/cc-unavailable diagnosis that storms today."""
    return DiagnosisResult(
        likely_cause="CC diagnosis unavailable — cannot determine root cause safely",
        confidence_pct=0,
        evidence=[],
        recommended_action=RecoveryAction.ESCALATE,
        reasoning="CC unavailable",
        source="cc_unavailable",
    )


def _resolved_diagnosis() -> DiagnosisResult:
    """CC reports it already fixed the issue (outcome=resolved)."""
    return DiagnosisResult(
        likely_cause="transient blip",
        confidence_pct=90,
        evidence=[],
        recommended_action=RecoveryAction.RESTART_SERVICES,
        reasoning="CC self-resolved",
        source="cc",
        outcome="resolved",
        actions_taken=["restart_services"],
    )


def _seed_state(config: GuardianConfig, **fields) -> Path:
    """Write a minimal state.json so run_check loads it (from_dict fills defaults)."""
    import json

    config.state_path.mkdir(parents=True, exist_ok=True)
    state_path = config.state_path / "state.json"
    state_path.write_text(json.dumps(fields))
    return state_path


def _alert_titles(mock_dispatcher: MagicMock) -> list[str]:
    """Titles of every Alert passed to dispatcher.send (Alert is the positional arg)."""
    return [call.args[0].title for call in mock_dispatcher.send.call_args_list]


class TestGuardianAlertOnce:
    """GUARD-R2-01 — alert once per down-episode + a single 'restored' ping.

    Minimal scope: stop the every-30s storm (re-diagnosis + re-alert) and add the
    currently-missing restored notification for autonomous recovery.
    """

    @pytest.mark.asyncio
    async def test_handle_confirmed_dead_skips_when_down_alert_already_sent(
        self, config: GuardianConfig
    ) -> None:
        """Repeat ticks must NOT re-diagnose or re-alert once the episode was handled."""
        sm = ConfirmationStateMachine(config)
        sm._state.current_state = GuardianState.CONFIRMED_DEAD
        sm._state.down_alert_sent = True

        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        diagnosis_engine = MagicMock()
        diagnosis_engine.diagnose = AsyncMock()
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        await _handle_confirmed_dead(
            config, sm, dispatcher, diagnosis_engine, recovery_engine
        )

        diagnosis_engine.diagnose.assert_not_called()  # no expensive Opus re-run
        dispatcher.send.assert_not_called()  # no re-alert
        recovery_engine.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_confirmed_dead_proceeds_and_marks_flag_when_clear(
        self, config: GuardianConfig
    ) -> None:
        """First handling (flag clear) diagnoses, then marks the episode as alerted."""
        sm = ConfirmationStateMachine(config)
        sm._state.current_state = GuardianState.CONFIRMED_DEAD
        sm._state.down_alert_sent = False
        sm._state.first_failure_at = _now_iso()

        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        diagnosis_engine = MagicMock()
        diagnosis_engine.diagnose = AsyncMock(return_value=_cc_unavailable_diagnosis())
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        with (
            patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
            patch("genesis.guardian.check.write_diagnosis_result"),
        ):
            await _handle_confirmed_dead(
                config, sm, dispatcher, diagnosis_engine, recovery_engine
            )

        diagnosis_engine.diagnose.assert_awaited_once()
        assert sm.state.down_alert_sent is True  # episode now marked — next tick skips

    @pytest.mark.asyncio
    async def test_autonomous_recovery_sends_one_restored_ping_and_clears_flag(
        self, config: GuardianConfig
    ) -> None:
        """Container returns on its own from CONFIRMED_DEAD → exactly one restored ping."""
        state_path = _seed_state(
            config,
            current_state="confirmed_dead",
            down_alert_sent=True,
            first_failure_at=_now_iso(),
        )
        mock_dispatcher = MagicMock()
        mock_dispatcher.send = AsyncMock()

        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_healthy_snapshot()),
            ),
            patch("genesis.guardian.check._build_dispatcher", return_value=mock_dispatcher),
            patch("genesis.guardian.check._handle_healthy", AsyncMock()),
            patch("genesis.guardian.check._write_guardian_heartbeat", AsyncMock()),
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)

        titles = _alert_titles(mock_dispatcher)
        restored = [t for t in titles if "restored" in t.lower() or "recovered" in t.lower()]
        assert len(restored) == 1, f"expected one restored ping, got titles={titles}"

        import json

        saved = json.loads(state_path.read_text())
        assert saved["current_state"] == "healthy"
        assert saved["down_alert_sent"] is False  # cleared on genuine recovery

    @pytest.mark.asyncio
    async def test_auto_reset_does_not_ping_or_clear_flag(
        self, config: GuardianConfig
    ) -> None:
        """Auto-reset (still-down) must NOT emit a restored ping nor clear the flag.

        The auto-reset trap: auto-reset routes CONFIRMED_DEAD→HEALTHY while the
        container is STILL down (all_alive=False). A naive 'clear on recovery'
        would wipe the throttle here and re-enable the storm.
        """
        state_path = _seed_state(
            config,
            current_state="confirmed_dead",
            down_alert_sent=True,
            first_failure_at="2026-01-01T00:00:00+00:00",  # long past timeout → auto-reset
            auto_reset_count=0,
        )
        mock_dispatcher = MagicMock()
        mock_dispatcher.send = AsyncMock()

        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_dead_snapshot()),
            ),
            patch("genesis.guardian.check._build_dispatcher", return_value=mock_dispatcher),
            patch("genesis.guardian.check._handle_healthy", AsyncMock()),
            patch("genesis.guardian.check._write_guardian_heartbeat", AsyncMock()),
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)

        titles = _alert_titles(mock_dispatcher)
        assert not any(
            "restored" in t.lower() or "recovered" in t.lower() for t in titles
        ), f"auto-reset must not ping restored; titles={titles}"

        import json

        saved = json.loads(state_path.read_text())
        assert saved["down_alert_sent"] is True  # retained — storm stays suppressed

    @pytest.mark.asyncio
    async def test_storm_suppressed_across_ticks(self, config: GuardianConfig) -> None:
        """Two CONFIRMED_DEAD ticks: alert on the first, silence on the second."""
        _seed_state(
            config,
            current_state="confirmed_dead",
            down_alert_sent=False,
            first_failure_at=_now_iso(),  # recent → no auto-reset
        )
        mock_dispatcher = MagicMock()
        mock_dispatcher.send = AsyncMock()

        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_dead_snapshot()),
            ),
            patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
            patch("genesis.guardian.check.write_diagnosis_result"),
            patch.object(
                DiagnosisEngine, "diagnose",
                AsyncMock(return_value=_cc_unavailable_diagnosis()),
            ),
            patch("genesis.guardian.check._build_dispatcher", return_value=mock_dispatcher),
            patch("genesis.guardian.check._write_guardian_heartbeat", AsyncMock()),
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)  # tick 1 — should alert
            count_after_tick1 = mock_dispatcher.send.call_count
            await run_check(config)  # tick 2 — should stay quiet
            count_after_tick2 = mock_dispatcher.send.call_count

        assert count_after_tick1 >= 1, "first down tick must alert"
        assert count_after_tick2 == count_after_tick1, (
            "second tick must NOT re-alert (storm killed)"
        )

    @pytest.mark.asyncio
    async def test_diagnosis_crash_unmarks_flag_for_retry(
        self, config: GuardianConfig
    ) -> None:
        """If diagnosis raises before any alert, the flag is un-marked so the
        next tick retries instead of silently muting the episode."""
        sm = ConfirmationStateMachine(config)
        sm._state.current_state = GuardianState.CONFIRMED_DEAD
        sm._state.down_alert_sent = False

        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        diagnosis_engine = MagicMock()
        diagnosis_engine.diagnose = AsyncMock(side_effect=RuntimeError("cc crashed"))
        recovery_engine = MagicMock()
        recovery_engine.execute = AsyncMock()

        with (
            patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
            pytest.raises(RuntimeError, match="cc crashed"),
        ):
            await _handle_confirmed_dead(
                config, sm, dispatcher, diagnosis_engine, recovery_engine
            )

        assert sm.state.down_alert_sent is False  # un-marked → next tick retries

    @pytest.mark.asyncio
    async def test_cc_resolved_recovery_clears_flag(self, config: GuardianConfig) -> None:
        """CC-auto-resolved recovery clears the flag at its recovery point (no leak)."""
        sm = ConfirmationStateMachine(config)
        sm._state.current_state = GuardianState.CONFIRMED_DEAD
        sm._state.down_alert_sent = True

        dispatcher = MagicMock()
        dispatcher.send = AsyncMock()
        recovery_engine = MagicMock()

        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_healthy_snapshot()),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await _handle_cc_resolved(
                config, sm, dispatcher, _resolved_diagnosis(), recovery_engine
            )

        assert sm.current_state == GuardianState.HEALTHY
        assert sm.state.down_alert_sent is False

    @pytest.mark.asyncio
    async def test_full_episode_is_two_pings_regardless_of_duration(
        self, config: GuardianConfig
    ) -> None:
        """Acceptance (user requirement): one 'down' + one 'restored' per episode,
        no matter how many ticks it stays down."""
        _seed_state(
            config,
            current_state="confirmed_dead",
            down_alert_sent=False,
            first_failure_at=_now_iso(),
        )
        mock_dispatcher = MagicMock()
        mock_dispatcher.send = AsyncMock()

        # Three consecutive down ticks (CC unavailable).
        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_dead_snapshot()),
            ),
            patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
            patch("genesis.guardian.check.write_diagnosis_result"),
            patch.object(
                DiagnosisEngine, "diagnose",
                AsyncMock(return_value=_cc_unavailable_diagnosis()),
            ),
            patch("genesis.guardian.check._build_dispatcher", return_value=mock_dispatcher),
            patch("genesis.guardian.check._write_guardian_heartbeat", AsyncMock()),
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)
            down_side_alerts = mock_dispatcher.send.call_count
            await run_check(config)
            await run_check(config)

        # Down-side alert count did NOT grow over the extra ticks.
        assert mock_dispatcher.send.call_count == down_side_alerts
        assert down_side_alerts >= 1

        # Now it recovers on its own.
        with (
            patch(
                "genesis.guardian.check.collect_all_signals",
                AsyncMock(return_value=_healthy_snapshot()),
            ),
            patch("genesis.guardian.check._build_dispatcher", return_value=mock_dispatcher),
            patch("genesis.guardian.check._handle_healthy", AsyncMock()),
            patch("genesis.guardian.check._write_guardian_heartbeat", AsyncMock()),
            patch("genesis.guardian.check.load_secrets", return_value={}),
        ):
            await run_check(config)

        titles = _alert_titles(mock_dispatcher)
        restored = [t for t in titles if "restored" in t.lower() or "recovered" in t.lower()]
        assert len(restored) == 1, f"expected exactly one restored ping; titles={titles}"
