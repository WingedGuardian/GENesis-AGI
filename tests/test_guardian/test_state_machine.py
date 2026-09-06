"""Tests for Guardian confirmation state machine."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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
    StateData,
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
        # Simulate that record_recovery_attempt() was called during execute()
        sm.record_recovery_attempt()
        assert sm.state.recovery_attempts == 1
        t = sm.process(_dead_snapshot(["container_exists"]))
        assert t.new_state == GuardianState.CONFIRMED_DEAD
        assert t.action_needed is True
        # recovery_attempts unchanged by _from_recovered — only record_recovery_attempt increments
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


class TestDownAlertFlag:
    """GUARD-R2-01 — the per-episode 'down alert already sent' flag (alert once)."""

    def test_defaults_false(self, sm: ConfirmationStateMachine) -> None:
        assert sm.state.down_alert_sent is False

    def test_mark_and_clear(self, sm: ConfirmationStateMachine) -> None:
        sm.mark_down_alert_sent()
        assert sm.state.down_alert_sent is True
        sm.clear_down_alert_sent()
        assert sm.state.down_alert_sent is False

    def test_persists_across_save_load(
        self, config: GuardianConfig, tmp_path: Path,
    ) -> None:
        """The flag MUST survive between oneshot invocations (state.json)."""
        sm = ConfirmationStateMachine(config)
        sm.mark_down_alert_sent()
        state_file = tmp_path / "state.json"
        sm.save_state(state_file)

        sm2 = ConfirmationStateMachine(config)
        sm2.load_state(state_file)
        assert sm2.state.down_alert_sent is True

    def test_defaults_false_for_legacy_state(self) -> None:
        """Old state.json without the key loads as False (backward compatible)."""
        sd = StateData.from_dict({"current_state": "confirmed_dead"})
        assert sd.down_alert_sent is False

    def test_not_cleared_by_reset_to_healthy(
        self, sm: ConfirmationStateMachine,
    ) -> None:
        """_reset_to_healthy (shared by auto-reset) must NOT clear the flag —
        that would let auto-reset oscillation re-enable the storm."""
        sm.mark_down_alert_sent()
        sm._reset_to_healthy("2026-06-16T00:00:00+00:00")
        assert sm.state.down_alert_sent is True


class TestRebootResetsTheEpisode:
    """The Guardian's escalation ladder must not resume mid-climb after a host
    reboot.

    `load_state` restores StateData verbatim (fresh only on corruption), and the
    300s bootstrap grace is consulted from exactly ONE transition —
    CONFIRMING->SURVEYING. So after a reboot: past CONFIRMING the grace is never
    reached, and inside CONFIRMING `elapsed` is computed from the persisted
    PRE-reboot `first_failure_at` and is already expired on arrival. Meanwhile
    the container legitimately IS bootstrapping and needs the full window.

    Resetting to HEALTHY re-arms the grace that already works, via the normal
    HEALTHY->SIGNAL_DROPPED->CONFIRMING path — rather than adding a second
    mechanism beside it.
    """

    BOOT_A = "1a2b3c4d-0000-4111-8000-000000000001"
    BOOT_B = "9f1c2d3e-0000-4444-8888-aaaabbbbcccc"

    def _rebooted(self, sm: ConfirmationStateMachine, *, was: GuardianState) -> bool:
        sm._state.current_state = was
        sm._state.boot_id = self.BOOT_A
        return sm.reset_if_rebooted(self.BOOT_B)

    def test_reboot_resets_the_episode(self, sm: ConfirmationStateMachine) -> None:
        sm._state.first_failure_at = "2026-03-25T11:50:00+00:00"
        sm._state.consecutive_failures = 4
        sm._state.recheck_count = 3
        assert self._rebooted(sm, was=GuardianState.CONFIRMED_DEAD) is True
        assert sm.state.current_state is GuardianState.HEALTHY
        assert sm.state.first_failure_at is None
        assert sm.state.consecutive_failures == 0
        assert sm.state.recheck_count == 0

    def test_same_boot_leaves_an_in_flight_episode_alone(
        self, sm: ConfirmationStateMachine,
    ) -> None:
        sm._state.current_state = GuardianState.SURVEYING
        sm._state.boot_id = self.BOOT_A
        assert sm.reset_if_rebooted(self.BOOT_A) is False
        assert sm.state.current_state is GuardianState.SURVEYING

    def test_legacy_state_adopts_the_boot_id_without_resetting(
        self, sm: ConfirmationStateMachine,
    ) -> None:
        # A state.json written before this field existed has boot_id == "".
        # Treating that as a reboot would wipe a live episode on the very tick
        # the new code first runs, on every install.
        sm._state.current_state = GuardianState.SURVEYING
        assert sm.state.boot_id == ""
        assert sm.reset_if_rebooted(self.BOOT_B) is False
        assert sm.state.current_state is GuardianState.SURVEYING
        assert sm.state.boot_id == self.BOOT_B

    def test_unreadable_boot_time_does_not_reset(
        self, sm: ConfirmationStateMachine,
    ) -> None:
        # Fail OPEN. A false positive silently discards an in-flight episode,
        # which is worse than carrying a stale one for another tick.
        sm._state.current_state = GuardianState.SURVEYING
        sm._state.boot_id = self.BOOT_A
        assert sm.reset_if_rebooted(None) is False
        assert sm.state.current_state is GuardianState.SURVEYING

    def test_reboot_clears_dialogue_state(self, sm: ConfirmationStateMachine) -> None:
        # The dialogue counterparty lives in the container and is gone across a
        # reboot; a stale sentinel_state would later be read as "proceed".
        sm._state.dialogue_sent_at = "2026-03-25T11:50:00+00:00"
        sm._state.dialogue_eta_s = 120
        sm._state.dialogue_action = "self_heal"
        sm._state.sentinel_state = "investigating"
        self._rebooted(sm, was=GuardianState.AWAITING_SELF_HEAL)
        assert sm.state.dialogue_sent_at is None
        assert sm.state.dialogue_eta_s == 0
        assert sm.state.dialogue_action is None
        assert sm.state.sentinel_state == ""

    # ── the bounded behaviours a reset must NOT re-enable ──────────────

    def test_reboot_preserves_recovery_attempts(
        self, sm: ConfirmationStateMachine,
    ) -> None:
        """RESTART_CONTAINER and SNAPSHOT_ROLLBACK *cause* reboots. Zeroing the
        escalation budget on a reboot the Guardian itself triggered turns a
        bounded 3-rung ladder into an unbounded loop."""
        sm._state.recovery_attempts = 2
        sm._state.last_recovery_at = "2026-03-25T11:50:00+00:00"
        self._rebooted(sm, was=GuardianState.RECOVERING)
        assert sm.state.recovery_attempts == 2
        assert sm.state.last_recovery_at == "2026-03-25T11:50:00+00:00"

    def test_reboot_preserves_io_triage_budget(
        self, sm: ConfirmationStateMachine,
    ) -> None:
        """IO_TRIAGE kills processes. Its budget resetting on every reboot would
        grant unlimited kills to a box in a reboot loop."""
        sm._state.io_triage_attempts = 4
        self._rebooted(sm, was=GuardianState.RECOVERING)
        assert sm.state.io_triage_attempts == 4

    def test_reboot_preserves_auto_reset_count(
        self, sm: ConfirmationStateMachine,
    ) -> None:
        """The confirmed-dead oscillation guard, capped at max_auto_resets."""
        sm._state.auto_reset_count = 2
        self._rebooted(sm, was=GuardianState.CONFIRMED_DEAD)
        assert sm.state.auto_reset_count == 2

    def test_reboot_preserves_down_alert_latch(
        self, sm: ConfirmationStateMachine,
    ) -> None:
        """GUARD-R2-01's one-alert-per-episode latch. Clearing it on a reboot
        sends a fresh CRITICAL every 30s to a box in a reboot loop — which is
        exactly the situation where reboots are observed."""
        sm.mark_down_alert_sent()
        self._rebooted(sm, was=GuardianState.CONFIRMED_DEAD)
        assert sm.state.down_alert_sent is True

    def test_reboot_preserves_the_snapshot_size_baseline(
        self, sm: ConfirmationStateMachine,
    ) -> None:
        """Not a counter — the disk-space baseline gating safe_to_snapshot().
        It is also aliased by reference from check.py and recovery.py, so the
        SAME list object must survive, not just equal contents."""
        history = sm._state.snapshot_size_history
        history.extend([4096, 8192, 49430528])
        self._rebooted(sm, was=GuardianState.RECOVERING)
        assert sm.state.snapshot_size_history == [4096, 8192, 49430528]
        assert sm.state.snapshot_size_history is history, (
            "re-binding a fresh list detaches the aliases held by check.py/recovery.py"
        )

    def test_reboot_preserves_pause_state_through_the_next_tick(
        self, sm: ConfirmationStateMachine,
    ) -> None:
        """User sovereignty: resetting these re-arms an already-sent reminder.

        Asserted through a following `process()`, not just on the field. Forcing
        current_state to HEALTHY would make `_handle_paused` treat the next tick
        as a NEW pause and rewrite paused_since — so a field-only assertion would
        pass while the value survived exactly zero ticks.
        """
        # A RECENT pause, so the 24h long-pause reminder does not legitimately
        # fire and overwrite last_pause_reminder_at — that would be correct
        # behaviour masquerading as a failure of the thing under test.
        paused_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        sm._state.paused_since = paused_at
        sm._state.last_pause_reminder_at = None
        self._rebooted(sm, was=GuardianState.PAUSED)
        sm.process(_paused_snapshot())
        assert sm.state.paused_since == paused_at, (
            "forcing PAUSED to HEALTHY makes the next tick read as a NEW pause, "
            "restarting the long-pause reminder clock on every host reboot"
        )

    def test_reboot_clears_stale_signal_history(
        self, sm: ConfirmationStateMachine,
    ) -> None:
        # check.py quotes signal_history[-5:] into three alert bodies. Carrying
        # pre-reboot signals across makes a post-reboot alert read as if the
        # episode were contiguous.
        sm._state.signal_history.append(
            {"at": "2026-03-25T11:50:00+00:00", "all_alive": False, "failed": ["health_api"]},
        )
        self._rebooted(sm, was=GuardianState.CONFIRMED_DEAD)
        assert sm.state.signal_history == []

    def test_reboot_clears_cc_unavailable_tracking(
        self, sm: ConfirmationStateMachine,
    ) -> None:
        # Tracks a CC outage episode on the HOST; the host just rebooted, so its
        # CC state is new.
        sm.set_cc_unavailable()
        sm.record_cc_unavailable_alert()
        self._rebooted(sm, was=GuardianState.CONFIRMED_DEAD)
        assert sm.state.cc_unavailable_since is None
        assert sm.state.last_cc_unavailable_alert_at is None

    def test_boot_id_survives_a_save_load_round_trip(
        self, config: GuardianConfig, tmp_path: Path,
    ) -> None:
        sm = ConfirmationStateMachine(config)
        sm._state.boot_id = self.BOOT_A
        state_file = tmp_path / "state.json"
        sm.save_state(state_file)

        sm2 = ConfirmationStateMachine(config)
        sm2.load_state(state_file)
        assert sm2.state.boot_id == self.BOOT_A

    def test_a_non_string_boot_id_on_disk_does_not_raise(
        self, config: GuardianConfig, tmp_path: Path,
    ) -> None:
        # The gateway's reset-state verb read-modify-writes this same JSON, and a
        # hand-edited file is plausible. An uncoerced value would raise inside the
        # comparison — before the heartbeat write and the finally: save_state.
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"current_state": "healthy", "boot_id": 12345}))
        sm = ConfirmationStateMachine(config)
        sm.load_state(state_file)
        assert sm.state.boot_id == "12345"
        assert sm.reset_if_rebooted(self.BOOT_A) is True

    def test_post_reboot_failure_gets_the_full_bootstrap_grace(
        self, sm: ConfirmationStateMachine,
    ) -> None:
        """The point of the reset: a fresh first_failure_at means CONFIRMING
        holds for the whole grace window instead of arriving already expired."""
        sm._state.first_failure_at = "2020-01-01T00:00:00+00:00"  # long expired
        self._rebooted(sm, was=GuardianState.CONFIRMING)
        sm.process(_dead_snapshot(["health_api"]))   # HEALTHY -> SIGNAL_DROPPED
        sm.process(_dead_snapshot(["health_api"]))   # -> CONFIRMING, fresh onset
        assert sm.state.current_state is GuardianState.CONFIRMING
        assert sm._is_within_bootstrap_grace() is True
