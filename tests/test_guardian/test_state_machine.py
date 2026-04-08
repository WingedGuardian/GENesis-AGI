"""Tests for Guardian confirmation state machine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from genesis.guardian.config import GuardianConfig
from genesis.guardian.health_signals import (
    HealthSnapshot,
    PauseState,
    SignalResult,
)
from genesis.guardian.state_machine import (
    ConfirmationStateMachine,
    GuardianState,
)


@pytest.fixture
def config() -> GuardianConfig:
    return GuardianConfig()


@pytest.fixture
def sm(config: GuardianConfig) -> ConfirmationStateMachine:
    return ConfirmationStateMachine(config)


def _healthy_snapshot() -> HealthSnapshot:
    """All 5 probes alive."""
    return HealthSnapshot(
        signals={
            "container_exists": SignalResult("container_exists", True, 1.0, "running", "t"),
            "icmp_reachable": SignalResult("icmp_reachable", True, 1.0, "ok", "t"),
            "health_api": SignalResult("health_api", True, 1.0, "healthy", "t"),
            "heartbeat_canary": SignalResult("heartbeat_canary", True, 1.0, "alive", "t"),
            "log_freshness": SignalResult("log_freshness", True, 1.0, "fresh", "t"),
        },
        pause_state=PauseState(paused=False),
        collected_at="2026-03-25T12:00:00+00:00",
    )


def _dead_snapshot(failed: list[str] | None = None) -> HealthSnapshot:
    """Some probes dead."""
    failed = failed or ["container_exists", "icmp_reachable"]
    signals = {}
    for name in ["container_exists", "icmp_reachable", "health_api", "heartbeat_canary", "log_freshness"]:
        alive = name not in failed
        signals[name] = SignalResult(name, alive, 1.0, "ok" if alive else "down", "t")
    return HealthSnapshot(
        signals=signals,
        pause_state=PauseState(paused=False),
        collected_at="2026-03-25T12:00:00+00:00",
    )


def _paused_snapshot() -> HealthSnapshot:
    """All probes alive, Genesis paused."""
    snap = _healthy_snapshot()
    snap.pause_state = PauseState(paused=True, reason="testing", since="2026-03-25T12:00:00")
    return snap


# ── Basic State Transitions ─────────────────────────────────────────────


class TestHealthyState:

    def test_stays_healthy_when_all_alive(self, sm: ConfirmationStateMachine) -> None:
        t = sm.process(_healthy_snapshot())
        assert t.new_state == GuardianState.HEALTHY
        assert t.changed is False
        assert t.action_needed is False

    def test_drops_signal_on_failure(self, sm: ConfirmationStateMachine) -> None:
        t = sm.process(_dead_snapshot(["container_exists"]))
        assert t.new_state == GuardianState.SIGNAL_DROPPED
        assert t.changed is True
        assert sm.state.consecutive_failures == 1
        assert sm.state.first_failure_at is not None


class TestSignalDroppedState:

    def test_recovers_on_next_healthy(self, sm: ConfirmationStateMachine) -> None:
        sm.process(_dead_snapshot(["container_exists"]))
        assert sm.current_state == GuardianState.SIGNAL_DROPPED

        t = sm.process(_healthy_snapshot())
        assert t.new_state == GuardianState.HEALTHY
        assert "transient" in t.reason

    def test_advances_to_confirming(self, sm: ConfirmationStateMachine) -> None:
        sm.process(_dead_snapshot(["container_exists"]))
        assert sm.current_state == GuardianState.SIGNAL_DROPPED

        t = sm.process(_dead_snapshot(["container_exists"]))
        assert t.new_state == GuardianState.CONFIRMING
        assert sm.state.recheck_count == 1


class TestConfirmingState:

    def test_recovers_during_confirmation(self, sm: ConfirmationStateMachine) -> None:
        sm.process(_dead_snapshot(["container_exists", "icmp_reachable"]))
        sm.process(_dead_snapshot(["container_exists", "icmp_reachable"]))
        assert sm.current_state == GuardianState.CONFIRMING

        t = sm.process(_healthy_snapshot())
        assert t.new_state == GuardianState.HEALTHY

    def test_advances_to_surveying(self, sm: ConfirmationStateMachine) -> None:
        # Config: max_recheck_attempts=3, required_failed_signals=2
        # Set first_failure_at to past to bypass bootstrap grace (300s)
        sm.process(_dead_snapshot(["container_exists", "icmp_reachable"]))  # → SIGNAL_DROPPED
        sm._state.first_failure_at = "2026-03-25T11:50:00+00:00"  # 10 min ago
        sm.process(_dead_snapshot(["container_exists", "icmp_reachable"]))  # → CONFIRMING (recheck 1)
        sm.process(_dead_snapshot(["container_exists", "icmp_reachable"]))  # recheck 2
        t = sm.process(_dead_snapshot(["container_exists", "icmp_reachable"]))  # recheck 3

        assert t.new_state == GuardianState.SURVEYING
        assert t.action_needed is True

    def test_heartbeat_only_counts_as_enough(self, sm: ConfirmationStateMachine) -> None:
        """Heartbeat-only failure should escalate with only 1 signal down."""
        sm.process(_dead_snapshot(["heartbeat_canary"]))  # → SIGNAL_DROPPED
        sm._state.first_failure_at = "2026-03-25T11:50:00+00:00"  # 10 min ago
        sm.process(_dead_snapshot(["heartbeat_canary"]))  # → CONFIRMING
        sm.process(_dead_snapshot(["heartbeat_canary"]))  # recheck 2
        t = sm.process(_dead_snapshot(["heartbeat_canary"]))  # recheck 3

        assert t.new_state == GuardianState.SURVEYING
        assert t.action_needed is True

    def test_single_non_heartbeat_needs_two_signals(self, sm: ConfirmationStateMachine) -> None:
        """Single non-heartbeat failure should NOT escalate (needs 2 signals)."""
        sm.process(_dead_snapshot(["icmp_reachable"]))  # → SIGNAL_DROPPED
        sm.process(_dead_snapshot(["icmp_reachable"]))  # → CONFIRMING
        sm.process(_dead_snapshot(["icmp_reachable"]))  # recheck 2
        t = sm.process(_dead_snapshot(["icmp_reachable"]))  # recheck 3

        # Still confirming — only 1 signal down, need 2
        assert t.new_state == GuardianState.CONFIRMING


# ── Recovery States ──────────────────────────────────────────────────────


class TestRecoveryStates:

    def test_confirmed_dead_needs_action(self, sm: ConfirmationStateMachine) -> None:
        sm._state.current_state = GuardianState.CONFIRMED_DEAD
        t = sm.process(_dead_snapshot(["container_exists"]))
        assert t.new_state == GuardianState.CONFIRMED_DEAD
        assert t.action_needed is True

    def test_confirmed_dead_auto_recovers_when_healthy(self, sm: ConfirmationStateMachine) -> None:
        sm._state.current_state = GuardianState.CONFIRMED_DEAD
        sm._state.consecutive_failures = 19
        sm._state.first_failure_at = "2026-04-06T18:24:58+00:00"
        sm._state.recovery_attempts = 2
        t = sm.process(_healthy_snapshot())
        assert t.new_state == GuardianState.HEALTHY
        assert "auto-recovered" in t.reason
        # Verify failure tracking was fully reset
        assert sm.state.consecutive_failures == 0
        assert sm.state.first_failure_at is None
        assert sm.state.recovery_attempts == 0

    def test_recovered_verifies_healthy(self, sm: ConfirmationStateMachine) -> None:
        sm._state.current_state = GuardianState.RECOVERED
        t = sm.process(_healthy_snapshot())
        assert t.new_state == GuardianState.HEALTHY
        assert "verified" in t.reason

    def test_recovered_fails_verification(self, sm: ConfirmationStateMachine) -> None:
        sm._state.current_state = GuardianState.RECOVERED
        t = sm.process(_dead_snapshot(["container_exists"]))
        assert t.new_state == GuardianState.CONFIRMED_DEAD
        assert t.action_needed is True
        assert sm.state.recovery_attempts == 1

    def test_escalation_check(self, sm: ConfirmationStateMachine) -> None:
        sm._state.recovery_attempts = 0
        assert sm.should_escalate() is False
        sm._state.recovery_attempts = 3  # max_escalations default
        assert sm.should_escalate() is True


# ── External State Manipulation ──────────────────────────────────────────


class TestExternalManipulation:

    def test_set_surveying(self, sm: ConfirmationStateMachine) -> None:
        sm.set_surveying()
        assert sm.current_state == GuardianState.SURVEYING

    def test_set_confirmed_dead(self, sm: ConfirmationStateMachine) -> None:
        sm.set_confirmed_dead()
        assert sm.current_state == GuardianState.CONFIRMED_DEAD

    def test_set_recovering(self, sm: ConfirmationStateMachine) -> None:
        sm.set_recovering()
        assert sm.current_state == GuardianState.RECOVERING

    def test_set_recovered(self, sm: ConfirmationStateMachine) -> None:
        sm.set_recovered()
        assert sm.current_state == GuardianState.RECOVERED


# ── Pause Handling ───────────────────────────────────────────────────────


class TestPauseHandling:

    def test_enters_paused_state(self, sm: ConfirmationStateMachine) -> None:
        t = sm.process(_paused_snapshot())
        assert t.new_state == GuardianState.PAUSED
        assert t.changed is True
        assert sm.state.paused_since is not None

    def test_stays_paused(self, sm: ConfirmationStateMachine) -> None:
        sm.process(_paused_snapshot())
        t = sm.process(_paused_snapshot())
        assert t.new_state == GuardianState.PAUSED
        assert t.changed is False

    def test_unpauses(self, sm: ConfirmationStateMachine) -> None:
        sm.process(_paused_snapshot())
        assert sm.current_state == GuardianState.PAUSED

        t = sm.process(_healthy_snapshot())
        assert t.new_state == GuardianState.HEALTHY
        assert "unpaused" in t.reason

    def test_infrastructure_failure_while_paused(self, sm: ConfirmationStateMachine) -> None:
        """Container down while paused should still alarm."""
        sm.process(_paused_snapshot())
        assert sm.current_state == GuardianState.PAUSED

        # Container dies while paused
        snap = _dead_snapshot(["container_exists"])
        snap.pause_state = PauseState(paused=True, reason="testing")
        t = sm.process(snap)
        assert t.new_state == GuardianState.SIGNAL_DROPPED
        assert t.action_needed is True


# ── State Persistence ────────────────────────────────────────────────────


class TestStatePersistence:

    def test_save_and_load(self, sm: ConfirmationStateMachine, state_dir: Path) -> None:
        state_file = state_dir / "state.json"

        # Generate some state
        sm.process(_dead_snapshot(["container_exists"]))
        sm.save_state(state_file)

        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["current_state"] == "signal_dropped"
        assert data["consecutive_failures"] == 1

    def test_load_restores_state(self, config: GuardianConfig, state_dir: Path) -> None:
        state_file = state_dir / "state.json"
        state_file.write_text(json.dumps({
            "current_state": "confirming",
            "consecutive_failures": 3,
            "recheck_count": 2,
            "first_failure_at": "2026-03-25T12:00:00",
            "last_healthy_at": "2026-03-25T11:59:00",
            "recovery_attempts": 0,
            "signal_history": [],
        }))

        sm = ConfirmationStateMachine(config)
        sm.load_state(state_file)
        assert sm.current_state == GuardianState.CONFIRMING
        assert sm.state.consecutive_failures == 3

    def test_corrupted_state_starts_fresh(
        self, config: GuardianConfig, state_dir: Path,
    ) -> None:
        state_file = state_dir / "state.json"
        state_file.write_text("not valid json{{{")

        sm = ConfirmationStateMachine(config)
        sm.load_state(state_file)
        assert sm.current_state == GuardianState.HEALTHY
        assert sm.state.consecutive_failures == 0

    def test_missing_state_starts_fresh(
        self, config: GuardianConfig, state_dir: Path,
    ) -> None:
        sm = ConfirmationStateMachine(config)
        sm.load_state(state_dir / "nonexistent.json")
        assert sm.current_state == GuardianState.HEALTHY

    def test_unknown_state_value_defaults(
        self, config: GuardianConfig, state_dir: Path,
    ) -> None:
        state_file = state_dir / "state.json"
        state_file.write_text(json.dumps({"current_state": "bogus_state"}))

        sm = ConfirmationStateMachine(config)
        sm.load_state(state_file)
        assert sm.current_state == GuardianState.HEALTHY

    def test_history_limited_to_20(self, sm: ConfirmationStateMachine) -> None:
        for _ in range(25):
            sm.process(_healthy_snapshot())
        assert len(sm.state.signal_history) <= 20


# ── Bootstrap Grace Period ───────────────────────────────────────────────


class TestBootstrapGrace:

    def test_bootstrap_503_within_grace(self, sm: ConfirmationStateMachine) -> None:
        """503 bootstrapping within grace period stays in CONFIRMING."""
        # First failure
        sm.process(_dead_snapshot(["heartbeat_canary"]))
        sm.process(_dead_snapshot(["heartbeat_canary"]))
        # Set first_failure_at to now (within 300s grace)
        assert sm.current_state == GuardianState.CONFIRMING

        # Should stay in CONFIRMING (bootstrap grace)
        t = sm.process(_dead_snapshot(["heartbeat_canary"]))
        # Note: with default config (max_recheck_attempts=3), recheck 2 stays confirming
        assert t.new_state == GuardianState.CONFIRMING


# ── CC Unavailability Tracking ──────────────────────────────────────────


class TestCCUnavailabilityTracking:

    def test_set_cc_unavailable(self, sm: ConfirmationStateMachine) -> None:
        assert sm.state.cc_unavailable_since is None
        sm.set_cc_unavailable()
        assert sm.state.cc_unavailable_since is not None

    def test_set_cc_unavailable_idempotent(self, sm: ConfirmationStateMachine) -> None:
        """Second call doesn't overwrite the original timestamp."""
        sm.set_cc_unavailable()
        first_ts = sm.state.cc_unavailable_since
        sm.set_cc_unavailable()
        assert sm.state.cc_unavailable_since == first_ts

    def test_clear_cc_unavailable(self, sm: ConfirmationStateMachine) -> None:
        sm.set_cc_unavailable()
        sm.record_cc_unavailable_alert()
        sm.clear_cc_unavailable()
        assert sm.state.cc_unavailable_since is None
        assert sm.state.last_cc_unavailable_alert_at is None

    def test_reset_to_healthy_clears_cc_state(
        self, sm: ConfirmationStateMachine,
    ) -> None:
        sm.set_cc_unavailable()
        sm.record_cc_unavailable_alert()
        # Put into a failure state first, then recover to trigger _reset_to_healthy
        sm.process(_dead_snapshot(["health_api", "heartbeat_canary"]))
        assert sm.current_state == GuardianState.SIGNAL_DROPPED
        # Now process healthy — triggers transition back to HEALTHY via reset
        sm.process(_healthy_snapshot())
        assert sm.current_state == GuardianState.HEALTHY
        assert sm.state.cc_unavailable_since is None
        assert sm.state.last_cc_unavailable_alert_at is None

    def test_persistence_round_trip(
        self, config: GuardianConfig, tmp_path: Path,
    ) -> None:
        sm = ConfirmationStateMachine(config)
        sm.set_cc_unavailable()
        sm.record_cc_unavailable_alert()
        state_file = tmp_path / "state.json"
        sm.save_state(state_file)

        sm2 = ConfirmationStateMachine(config)
        sm2.load_state(state_file)
        assert sm2.state.cc_unavailable_since == sm.state.cc_unavailable_since
        assert sm2.state.last_cc_unavailable_alert_at == sm.state.last_cc_unavailable_alert_at
