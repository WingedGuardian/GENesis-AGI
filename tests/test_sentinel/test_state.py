"""Tests for genesis.sentinel.state — state machine and persistence."""

from __future__ import annotations

import json
from pathlib import Path

from genesis.sentinel.state import (
    SentinelState,
    SentinelStateData,
    load_state,
    save_state,
)


class TestSentinelState:
    def test_default_state_is_healthy(self):
        data = SentinelStateData()
        assert data.state == SentinelState.HEALTHY

    def test_transition_updates_state(self):
        data = SentinelStateData()
        data.transition(SentinelState.INVESTIGATING, reason="test alarm")
        assert data.state == SentinelState.INVESTIGATING
        assert data.current_state == "investigating"
        assert data.last_trigger_reason == "test alarm"
        assert data.entered_at != ""

    def test_transition_sequence(self):
        data = SentinelStateData()
        data.transition(SentinelState.INVESTIGATING)
        data.transition(SentinelState.REMEDIATING)
        data.transition(SentinelState.ESCALATED)
        assert data.state == SentinelState.ESCALATED

    def test_invalid_state_returns_healthy(self):
        data = SentinelStateData(current_state="bogus")
        assert data.state == SentinelState.HEALTHY


class TestRecordDispatch:
    """After Part 7, record_cc_dispatch is observability only — no cooldown,
    no daily budget. Cadence is governed by per-pattern exponential backoff
    in the dispatcher, not here.
    """

    def test_record_sets_last_dispatch_timestamp(self):
        data = SentinelStateData()
        assert data.last_cc_dispatch_at == ""
        data.record_cc_dispatch()
        assert data.last_cc_dispatch_at != ""
        # Should be a parseable ISO timestamp
        from datetime import datetime
        parsed = datetime.fromisoformat(data.last_cc_dispatch_at)
        assert parsed is not None

    def test_no_daily_budget_fields(self):
        """Daily budget removal: fields must not exist on the dataclass."""
        data = SentinelStateData()
        assert not hasattr(data, "cc_dispatches_today")
        assert not hasattr(data, "cc_dispatches_date")
        assert not hasattr(data, "cooldown_s")

    def test_no_cooldown_method(self):
        """Global cooldown removed — in_cooldown() should not exist."""
        data = SentinelStateData()
        assert not hasattr(data, "in_cooldown")


class TestAutoReset:
    def test_not_escalated_no_reset(self):
        data = SentinelStateData(current_state="healthy")
        assert not data.should_auto_reset_escalated()

    def test_escalated_past_timeout_resets(self):
        data = SentinelStateData(
            current_state="escalated",
            entered_at="2020-01-01T00:00:00+00:00",  # Long ago
            escalated_timeout_s=600,
            escalated_count=0,
            max_escalated_resets=3,
        )
        assert data.should_auto_reset_escalated()

    def test_oscillation_guard_prevents_reset(self):
        data = SentinelStateData(
            current_state="escalated",
            entered_at="2020-01-01T00:00:00+00:00",
            escalated_count=3,
            max_escalated_resets=3,
        )
        assert not data.should_auto_reset_escalated()


class TestPersistence:
    def test_save_and_load(self, tmp_path: Path):
        state_file = tmp_path / "sentinel_state.json"
        data = SentinelStateData(current_state="investigating")
        data.transition(SentinelState.REMEDIATING, reason="test")

        save_state(data, state_file)
        loaded = load_state(state_file)

        assert loaded.state == SentinelState.REMEDIATING
        assert loaded.last_trigger_reason == "test"

    def test_load_missing_file_returns_healthy(self, tmp_path: Path):
        loaded = load_state(tmp_path / "nonexistent.json")
        assert loaded.state == SentinelState.HEALTHY

    def test_load_corrupt_file_returns_healthy(self, tmp_path: Path):
        state_file = tmp_path / "sentinel_state.json"
        state_file.write_text("not json{{{")
        loaded = load_state(state_file)
        assert loaded.state == SentinelState.HEALTHY

    def test_save_is_atomic(self, tmp_path: Path):
        state_file = tmp_path / "sentinel_state.json"
        data = SentinelStateData(current_state="escalated")
        save_state(data, state_file)
        assert state_file.exists()
        parsed = json.loads(state_file.read_text())
        assert parsed["current_state"] == "escalated"
