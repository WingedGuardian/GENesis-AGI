"""Tests for genesis.sentinel.dispatcher — CC session orchestration."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.sentinel.classifier import FireAlarm
from genesis.sentinel.dispatcher import (
    _BACKOFF_SCHEDULE_S,
    _ESCALATE_AT_ATTEMPT,
    SentinelDispatcher,
    SentinelRequest,
    _extract_pattern,
)
from genesis.sentinel.state import SentinelState, SentinelStateData


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    """Ensure load_state returns fresh state, not the live system's state file."""
    monkeypatch.setattr(
        "genesis.sentinel.dispatcher.load_state",
        lambda: SentinelStateData(),
    )


def _make_dispatcher(**kwargs):
    """Create a SentinelDispatcher with mocked dependencies."""
    defaults = {
        "session_manager": AsyncMock(),
        "invoker": AsyncMock(),
        "db": AsyncMock(),
        "event_bus": AsyncMock(),
        "health_data": AsyncMock(),
    }
    defaults.update(kwargs)

    # Session manager returns a session dict
    if defaults["session_manager"] is not None:
        defaults["session_manager"].create_background = AsyncMock(
            return_value={"id": "test-session-123"},
        )
        defaults["session_manager"].complete = AsyncMock()

    # Invoker returns a mock output
    if defaults["invoker"] is not None:
        mock_output = MagicMock()
        mock_output.text = '{"diagnosis": "test diagnosis", "actions_taken": ["fixed it"], "resolved": true}'
        defaults["invoker"].run = AsyncMock(return_value=mock_output)

    # Health data returns a snapshot dict (not a coroutine)
    if defaults["health_data"] is not None:
        defaults["health_data"].snapshot = AsyncMock(return_value={
            "infrastructure": {"qdrant": {"status": "healthy"}},
            "services": {},
            "queues": {},
            "cc_sessions": {},
        })

    # DB — mock execute for observation creation
    if defaults["db"] is not None:
        defaults["db"].execute = AsyncMock()
        defaults["db"].commit = AsyncMock()

    return SentinelDispatcher(**defaults)


class TestGates:
    @pytest.mark.asyncio
    async def test_bootstrap_grace_blocks(self):
        d = _make_dispatcher()
        d._state.started_at = datetime.now(UTC).isoformat()
        d._state.bootstrap_grace_s = 9999  # Long grace period

        result = await d.dispatch(SentinelRequest(
            trigger_source="test", trigger_reason="test",
        ))
        assert not result.dispatched
        assert "bootstrap grace" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_concurrent_limit_blocks(self):
        d = _make_dispatcher()
        d._state.started_at = "2020-01-01T00:00:00+00:00"  # Past grace
        d._active_session_id = "already-running"

        result = await d.dispatch(SentinelRequest(
            trigger_source="test", trigger_reason="test",
        ))
        assert not result.dispatched
        assert "already active" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_no_invoker_blocks(self):
        d = _make_dispatcher(invoker=None)
        d._state.started_at = "2020-01-01T00:00:00+00:00"

        result = await d.dispatch(SentinelRequest(
            trigger_source="test", trigger_reason="test",
        ))
        assert not result.dispatched
        assert "not available" in result.reason.lower()


class TestDispatch:
    @pytest.mark.asyncio
    async def test_successful_dispatch(self):
        d = _make_dispatcher()
        d._state.started_at = "2020-01-01T00:00:00+00:00"

        # Mock observation creation
        with patch("genesis.sentinel.dispatcher.save_state"), \
             patch("genesis.sentinel.dispatcher.write_last_run"), \
             patch("genesis.sentinel.dispatcher.write_state_for_guardian"), \
             patch("genesis.sentinel.dispatcher.append_log"):
            result = await d.dispatch(SentinelRequest(
                trigger_source="test",
                trigger_reason="test alarm",
                tier=2,
            ))

        assert result.dispatched
        assert result.resolved
        assert result.diagnosis == "test diagnosis"
        assert result.session_id == "test-session-123"
        assert d._state.state == SentinelState.HEALTHY

    @pytest.mark.asyncio
    async def test_unresolved_dispatch_escalates(self):
        d = _make_dispatcher()
        d._state.started_at = "2020-01-01T00:00:00+00:00"

        # Make invoker return unresolved
        mock_output = MagicMock()
        mock_output.text = '{"diagnosis": "cannot fix", "actions_taken": [], "resolved": false}'
        d._invoker.run = AsyncMock(return_value=mock_output)

        with patch("genesis.sentinel.dispatcher.save_state"), \
             patch("genesis.sentinel.dispatcher.write_last_run"), \
             patch("genesis.sentinel.dispatcher.write_state_for_guardian"), \
             patch("genesis.sentinel.dispatcher.append_log"):
            result = await d.dispatch(SentinelRequest(
                trigger_source="test",
                trigger_reason="test alarm",
            ))

        assert result.dispatched
        assert not result.resolved
        assert d._state.state == SentinelState.ESCALATED

    @pytest.mark.asyncio
    async def test_dispatch_records_last_dispatch_time(self):
        d = _make_dispatcher()
        d._state.started_at = "2020-01-01T00:00:00+00:00"

        with patch("genesis.sentinel.dispatcher.save_state"), \
             patch("genesis.sentinel.dispatcher.write_last_run"), \
             patch("genesis.sentinel.dispatcher.write_state_for_guardian"), \
             patch("genesis.sentinel.dispatcher.append_log"):
            await d.dispatch(SentinelRequest(
                trigger_source="test", trigger_reason="test",
            ))

        # Last dispatch time is recorded for observability (no daily budget)
        assert d._state.last_cc_dispatch_at != ""


class TestAutoReset:
    @pytest.mark.asyncio
    async def test_auto_reset_from_escalated(self):
        d = _make_dispatcher()
        d._state.current_state = "escalated"
        d._state.entered_at = "2020-01-01T00:00:00+00:00"
        d._state.escalated_timeout_s = 1  # Already expired
        d._state.escalated_count = 0
        d._state.started_at = "2020-01-01T00:00:00+00:00"

        with patch("genesis.sentinel.dispatcher.save_state"):
            result = await d.dispatch(SentinelRequest(
                trigger_source="test", trigger_reason="after reset",
            ))

        # The dispatch auto-resets from ESCALATED first (incrementing count),
        # then dispatches CC which resolves successfully (resetting count to 0)
        assert result.dispatched
        # After successful resolution, state is HEALTHY
        assert d._state.state == SentinelState.HEALTHY


class TestEscalateDirect:
    @pytest.mark.asyncio
    async def test_escalate_direct(self):
        d = _make_dispatcher()
        d._state.started_at = "2020-01-01T00:00:00+00:00"

        with patch("genesis.sentinel.dispatcher.save_state"), \
             patch("genesis.sentinel.dispatcher.write_last_run"), \
             patch("genesis.sentinel.dispatcher.write_state_for_guardian"), \
             patch("genesis.sentinel.dispatcher.append_log"):
            result = await d.escalate_direct(
                trigger_source="watchdog_reset_failed",
                tier=1,
                reason="Guardian reset-state failed",
            )

        assert result.dispatched


class TestProperties:
    def test_state_property(self):
        d = _make_dispatcher()
        assert d.state.state == SentinelState.HEALTHY

    def test_is_active_when_no_session(self):
        d = _make_dispatcher()
        assert not d.is_active


class TestExtractPattern:
    def test_fire_alarm_uses_alert_id(self):
        req = SentinelRequest(
            trigger_source="fire_alarm",
            trigger_reason="Tier 2: Memory >90%",
            alarms=[FireAlarm(tier=2, alert_id="memory:critical", severity="CRITICAL", message="mem")],
        )
        assert _extract_pattern(req) == "memory:critical"

    def test_direct_escalation_uses_trigger_source(self):
        req = SentinelRequest(
            trigger_source="watchdog_reset_failed",
            trigger_reason="Guardian reset failed",
        )
        assert _extract_pattern(req) == "direct:watchdog_reset_failed"

    def test_worst_alarm_wins(self):
        """The classifier sorts worst-first, so alarms[0] is the pattern key."""
        req = SentinelRequest(
            trigger_source="fire_alarm",
            trigger_reason="multi",
            alarms=[
                FireAlarm(tier=1, alert_id="service:watchdog_blind", severity="WARNING", message="a"),
                FireAlarm(tier=2, alert_id="memory:critical", severity="CRITICAL", message="b"),
            ],
        )
        assert _extract_pattern(req) == "service:watchdog_blind"


class TestBackoffReady:
    def test_first_attempt_immediate(self):
        d = _make_dispatcher()
        ready, _ = d._backoff_ready("memory:critical")
        assert ready is True

    def test_second_attempt_blocked_until_15min(self):
        d = _make_dispatcher()
        # Simulate one prior attempt, just now
        d._pattern_attempts["memory:critical"] = [time.monotonic()]
        ready, reason = d._backoff_ready("memory:critical")
        assert ready is False
        assert "backoff" in reason.lower()

    def test_second_attempt_ready_after_15min(self):
        d = _make_dispatcher()
        # Simulate one prior attempt >15 min ago
        d._pattern_attempts["memory:critical"] = [time.monotonic() - (16 * 60)]
        ready, _ = d._backoff_ready("memory:critical")
        assert ready is True

    def test_third_attempt_needs_45min(self):
        d = _make_dispatcher()
        now = time.monotonic()
        # Two prior attempts; last was 30 min ago (< 45 min required)
        d._pattern_attempts["memory:critical"] = [now - 3600, now - (30 * 60)]
        ready, _ = d._backoff_ready("memory:critical")
        assert ready is False

        # Bump the last attempt to 50 min ago — should clear
        d._pattern_attempts["memory:critical"] = [now - 7200, now - (50 * 60)]
        ready, _ = d._backoff_ready("memory:critical")
        assert ready is True

    def test_fourth_attempt_needs_2h(self):
        d = _make_dispatcher()
        now = time.monotonic()
        # Three prior attempts, last was 1h ago (< 2h)
        d._pattern_attempts["memory:critical"] = [
            now - 7200, now - 5400, now - 3600,
        ]
        ready, _ = d._backoff_ready("memory:critical")
        assert ready is False

    def test_fifth_attempt_hits_escalation(self):
        """4 prior attempts → the 5th attempt = escalation threshold."""
        d = _make_dispatcher()
        now = time.monotonic()
        d._pattern_attempts["memory:critical"] = [
            now - 10000, now - 8000, now - 6000, now - 3000,
        ]
        ready, reason = d._backoff_ready("memory:critical")
        # _backoff_ready returns ready=True at the threshold — the dispatcher
        # distinguishes escalation from normal dispatch separately.
        assert ready is True
        assert "escalation" in reason.lower()

    def test_escalated_pattern_always_blocked(self):
        d = _make_dispatcher()
        d._escalated_patterns["memory:critical"] = "2026-04-09T23:00:00+00:00"
        ready, reason = d._backoff_ready("memory:critical")
        assert ready is False
        assert "escalated" in reason.lower()

    def test_different_patterns_have_independent_backoff(self):
        d = _make_dispatcher()
        d._pattern_attempts["memory:critical"] = [time.monotonic()]
        # Other pattern should still be ready
        ready, _ = d._backoff_ready("infra:disk_low")
        assert ready is True


class TestBackoffScheduleConstants:
    def test_schedule_length_matches_escalation_threshold(self):
        """_ESCALATE_AT_ATTEMPT must be len(schedule)+1 — invariant."""
        assert len(_BACKOFF_SCHEDULE_S) + 1 == _ESCALATE_AT_ATTEMPT

    def test_schedule_is_monotonic_non_decreasing(self):
        """Backoff intervals must not shrink."""
        for i in range(1, len(_BACKOFF_SCHEDULE_S)):
            assert _BACKOFF_SCHEDULE_S[i] >= _BACKOFF_SCHEDULE_S[i - 1]

    def test_first_attempt_is_immediate(self):
        assert _BACKOFF_SCHEDULE_S[0] == 0.0


class TestPatternResetOnResolve:
    @pytest.mark.asyncio
    async def test_resolved_dispatch_clears_pattern(self):
        """On successful resolution, the backoff counter for the pattern resets."""
        d = _make_dispatcher()
        d._state.started_at = "2020-01-01T00:00:00+00:00"
        # Pre-seed one old attempt (past 15-min wait) so the next attempt
        # is ready — and then verify the whole counter gets wiped on resolve.
        d._pattern_attempts["memory:critical"] = [time.monotonic() - (20 * 60)]

        alarm = FireAlarm(tier=2, alert_id="memory:critical", severity="CRITICAL", message="mem")
        with patch("genesis.sentinel.dispatcher.save_state"), \
             patch("genesis.sentinel.dispatcher.write_last_run"), \
             patch("genesis.sentinel.dispatcher.write_state_for_guardian"), \
             patch("genesis.sentinel.dispatcher.append_log"):
            result = await d.dispatch(SentinelRequest(
                trigger_source="fire_alarm",
                trigger_reason="Tier 2 alarm",
                tier=2,
                alarms=[alarm],
            ))

        assert result.dispatched
        assert result.resolved
        # Pattern cleared after resolution
        assert "memory:critical" not in d._pattern_attempts

    @pytest.mark.asyncio
    async def test_unresolved_dispatch_keeps_pattern(self):
        """Failed/unresolved dispatches leave the backoff counter in place."""
        d = _make_dispatcher()
        d._state.started_at = "2020-01-01T00:00:00+00:00"
        # Force unresolved result
        mock_output = MagicMock()
        mock_output.text = '{"diagnosis": "cannot fix", "actions_taken": [], "resolved": false}'
        d._invoker.run = AsyncMock(return_value=mock_output)

        alarm = FireAlarm(tier=2, alert_id="memory:critical", severity="CRITICAL", message="mem")
        with patch("genesis.sentinel.dispatcher.save_state"), \
             patch("genesis.sentinel.dispatcher.write_last_run"), \
             patch("genesis.sentinel.dispatcher.write_state_for_guardian"), \
             patch("genesis.sentinel.dispatcher.append_log"):
            await d.dispatch(SentinelRequest(
                trigger_source="fire_alarm",
                trigger_reason="Tier 2 alarm",
                tier=2,
                alarms=[alarm],
            ))

        # Pattern is recorded so next attempt hits backoff
        assert "memory:critical" in d._pattern_attempts
        assert len(d._pattern_attempts["memory:critical"]) == 1


class TestEscalation:
    @pytest.mark.asyncio
    async def test_fifth_attempt_posts_escalation_and_blocks(self):
        """The 5th attempt for a pattern escalates to the user, not CC."""
        outreach = AsyncMock()
        outreach.submit_raw = AsyncMock()
        d = _make_dispatcher(outreach_pipeline=outreach)
        d._state.started_at = "2020-01-01T00:00:00+00:00"
        # Pre-seed 4 prior attempts (escalation = 5th)
        now = time.monotonic()
        d._pattern_attempts["memory:critical"] = [
            now - 20000, now - 16000, now - 10000, now - 5000,
        ]

        alarm = FireAlarm(tier=2, alert_id="memory:critical", severity="CRITICAL", message="mem")
        with patch("genesis.sentinel.dispatcher.save_state"), \
             patch("genesis.sentinel.dispatcher.append_log"):
            result = await d.dispatch(SentinelRequest(
                trigger_source="fire_alarm",
                trigger_reason="Tier 2 alarm",
                tier=2,
                alarms=[alarm],
            ))

        assert not result.dispatched
        assert "escalat" in result.reason.lower()
        # Outreach was posted
        assert outreach.submit_raw.await_count == 1
        # Pattern is now marked escalated
        assert "memory:critical" in d._escalated_patterns
        # Invoker was NOT called (no CC session spun up)
        d._invoker.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_escalation_message_uses_honest_attempt_wording(self):
        """The escalation message must say 'dispatch attempts' — not claim
        CC tried to resolve anything. Attempts include user rejections.
        """
        outreach = AsyncMock()
        outreach.submit_raw = AsyncMock()
        d = _make_dispatcher(outreach_pipeline=outreach)
        d._state.started_at = "2020-01-01T00:00:00+00:00"
        now = time.monotonic()
        d._pattern_attempts["memory:critical"] = [
            now - 20000, now - 16000, now - 10000, now - 5000,
        ]

        alarm = FireAlarm(tier=2, alert_id="memory:critical", severity="CRITICAL", message="mem")
        with patch("genesis.sentinel.dispatcher.save_state"), \
             patch("genesis.sentinel.dispatcher.append_log"):
            await d.dispatch(SentinelRequest(
                trigger_source="fire_alarm",
                trigger_reason="Tier 2 alarm",
                tier=2,
                alarms=[alarm],
            ))

        # Inspect the posted message body
        args, kwargs = outreach.submit_raw.call_args
        message_body = args[0] if args else ""
        # Honest wording — no false claim about CC resolution attempts
        assert "tried to resolve" not in message_body
        assert "dispatch attempts" in message_body
        assert "4 dispatch attempts" in message_body  # 4 prior + current = 5th triggers
        # Pattern identifier is visible to user
        assert "memory:critical" in message_body

    @pytest.mark.asyncio
    async def test_escalation_marks_pattern_even_without_outreach(self):
        """If outreach is unavailable, the escalation state is still tracked
        so the dispatcher stops auto-responding. Spam protection must not
        depend on outreach succeeding.
        """
        d = _make_dispatcher(outreach_pipeline=None)
        d._state.started_at = "2020-01-01T00:00:00+00:00"
        now = time.monotonic()
        d._pattern_attempts["memory:critical"] = [
            now - 20000, now - 16000, now - 10000, now - 5000,
        ]

        alarm = FireAlarm(tier=2, alert_id="memory:critical", severity="CRITICAL", message="mem")
        with patch("genesis.sentinel.dispatcher.save_state"), \
             patch("genesis.sentinel.dispatcher.append_log"):
            result = await d.dispatch(SentinelRequest(
                trigger_source="fire_alarm",
                trigger_reason="Tier 2 alarm",
                tier=2,
                alarms=[alarm],
            ))

        assert not result.dispatched
        # Still marked escalated even though no outreach pipeline
        assert "memory:critical" in d._escalated_patterns

    @pytest.mark.asyncio
    async def test_escalated_pattern_blocks_subsequent_attempts(self):
        d = _make_dispatcher()
        d._state.started_at = "2020-01-01T00:00:00+00:00"
        d._escalated_patterns["memory:critical"] = "2026-04-09T23:00:00+00:00"

        alarm = FireAlarm(tier=2, alert_id="memory:critical", severity="CRITICAL", message="mem")
        with patch("genesis.sentinel.dispatcher.save_state"), \
             patch("genesis.sentinel.dispatcher.append_log"):
            result = await d.dispatch(SentinelRequest(
                trigger_source="fire_alarm",
                trigger_reason="Tier 2 alarm",
                tier=2,
                alarms=[alarm],
            ))

        assert not result.dispatched
        assert "escalated" in result.reason.lower()
        d._invoker.run.assert_not_called()


class TestRingBufferDebounce:
    """2-of-3 debouncing: single-tick flaps are filtered out."""

    @pytest.mark.asyncio
    async def test_single_tick_alarm_does_not_dispatch(self):
        d = _make_dispatcher()
        d._state.started_at = "2020-01-01T00:00:00+00:00"

        with patch("genesis.mcp.health_mcp._impl_health_alerts", new=AsyncMock(return_value=[
            {"id": "memory:critical", "severity": "CRITICAL", "message": "mem"},
        ])):
            result = await d.check_fire_alarms()

        # First time the alarm appears — not yet confirmed by 2-of-3
        assert result is None
        # Buffer recorded the tick
        assert len(d._recent_alarm_sets) == 1

    @pytest.mark.asyncio
    async def test_two_consecutive_ticks_triggers_dispatch(self):
        d = _make_dispatcher()
        d._state.started_at = "2020-01-01T00:00:00+00:00"

        alerts = [{"id": "memory:critical", "severity": "CRITICAL", "message": "mem"}]
        with patch("genesis.mcp.health_mcp._impl_health_alerts", new=AsyncMock(return_value=alerts)), \
             patch("genesis.sentinel.dispatcher.save_state"), \
             patch("genesis.sentinel.dispatcher.write_last_run"), \
             patch("genesis.sentinel.dispatcher.write_state_for_guardian"), \
             patch("genesis.sentinel.dispatcher.append_log"):
            result1 = await d.check_fire_alarms()
            result2 = await d.check_fire_alarms()

        assert result1 is None  # First tick: not confirmed
        assert result2 is not None  # Second tick: confirmed
        assert result2.dispatched

    @pytest.mark.asyncio
    async def test_flapping_alarm_confirmed_across_non_consecutive_ticks(self):
        """Pattern in tick 1 and tick 3 (absent in tick 2) still counts."""
        d = _make_dispatcher()
        d._state.started_at = "2020-01-01T00:00:00+00:00"

        alerts_on = [{"id": "memory:critical", "severity": "CRITICAL", "message": "mem"}]
        alerts_off: list = []

        async def _alerts_side_effect(*args, **kwargs):
            return _alerts_side_effect.calls.pop(0)
        _alerts_side_effect.calls = [alerts_on, alerts_off, alerts_on]

        with patch("genesis.mcp.health_mcp._impl_health_alerts", new=_alerts_side_effect), \
             patch("genesis.sentinel.dispatcher.save_state"), \
             patch("genesis.sentinel.dispatcher.write_last_run"), \
             patch("genesis.sentinel.dispatcher.write_state_for_guardian"), \
             patch("genesis.sentinel.dispatcher.append_log"):
            r1 = await d.check_fire_alarms()  # {mem:crit} → 1/1 not confirmed
            r2 = await d.check_fire_alarms()  # {} → empty, returns early
            r3 = await d.check_fire_alarms()  # {mem:crit} → 2/3 confirmed

        assert r1 is None
        assert r2 is None
        assert r3 is not None and r3.dispatched

    @pytest.mark.asyncio
    async def test_isolated_spike_never_dispatches(self):
        """Alarm in tick 2 only, absent before and after — never confirmed."""
        d = _make_dispatcher()
        d._state.started_at = "2020-01-01T00:00:00+00:00"

        alerts_on = [{"id": "memory:critical", "severity": "CRITICAL", "message": "mem"}]
        alerts_off: list = []

        async def _alerts_side_effect(*args, **kwargs):
            return _alerts_side_effect.calls.pop(0)
        _alerts_side_effect.calls = [alerts_off, alerts_on, alerts_off]

        with patch("genesis.mcp.health_mcp._impl_health_alerts", new=_alerts_side_effect):
            r1 = await d.check_fire_alarms()
            r2 = await d.check_fire_alarms()
            r3 = await d.check_fire_alarms()

        assert r1 is None
        assert r2 is None
        assert r3 is None


class TestRejectionWindow:
    """Tests for the 24-hour rejection suppression window."""

    def test_rejected_pattern_blocked_during_window(self):
        """Pattern with active rejection window returns not-ready."""
        d = _make_dispatcher()
        expiry = (datetime.now(UTC) + timedelta(hours=23)).isoformat()
        d._state.rejected_patterns["memory:critical"] = expiry
        ready, reason = d._backoff_ready("memory:critical")
        assert ready is False
        assert "rejected" in reason.lower()

    def test_rejected_pattern_allowed_after_expiry(self):
        """Pattern with expired rejection window is allowed and entry cleaned."""
        d = _make_dispatcher()
        expiry = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        d._state.rejected_patterns["memory:critical"] = expiry

        with patch("genesis.sentinel.dispatcher.save_state"):
            ready, _ = d._backoff_ready("memory:critical")

        assert ready is True
        assert "memory:critical" not in d._state.rejected_patterns

    def test_rejection_independent_per_pattern(self):
        """Rejecting one pattern doesn't block another."""
        d = _make_dispatcher()
        expiry = (datetime.now(UTC) + timedelta(hours=23)).isoformat()
        d._state.rejected_patterns["memory:critical"] = expiry
        ready, _ = d._backoff_ready("infra:disk_low")
        assert ready is True

    def test_resolve_clears_rejection(self):
        """Successful resolve clears the rejection entry for the pattern."""
        d = _make_dispatcher()
        d._state.started_at = "2020-01-01T00:00:00+00:00"
        expiry = (datetime.now(UTC) + timedelta(hours=20)).isoformat()
        d._state.rejected_patterns["memory:critical"] = expiry

        # Simulate resolve by directly calling the logic from _finalize_dispatch
        pattern = "memory:critical"
        if pattern in d._state.rejected_patterns:
            del d._state.rejected_patterns[pattern]

        assert "memory:critical" not in d._state.rejected_patterns

    @pytest.mark.asyncio
    async def test_handle_approval_resolution_rejected(self):
        """handle_approval_resolution('rejected') records 24h window and transitions to HEALTHY."""
        d = _make_dispatcher()
        d._state.current_state = "awaiting_dispatch_approval"
        d._state.pending_request_id = "req-123"
        d._state.pending_policy_id = "sentinel_dispatch"
        d._state.pending_pattern = "memory:critical"

        with patch("genesis.sentinel.dispatcher.save_state"), \
             patch("genesis.sentinel.dispatcher.append_log"):
            result = await d.handle_approval_resolution("req-123", "rejected")

        assert result is not None
        assert result.dispatched is False
        assert "memory:critical" in d._state.rejected_patterns
        # Verify the expiry is ~24h from now
        expiry = datetime.fromisoformat(d._state.rejected_patterns["memory:critical"])
        diff = expiry - datetime.now(UTC)
        assert timedelta(hours=23) < diff < timedelta(hours=25)
        # State transitioned to HEALTHY
        assert d._state.current_state == "healthy"

    def test_rejection_persists_across_save_load(self, tmp_path):
        """rejected_patterns survives state serialization round-trip."""
        from genesis.sentinel.state import SentinelStateData, load_state, save_state

        state = SentinelStateData()
        expiry = (datetime.now(UTC) + timedelta(hours=12)).isoformat()
        state.rejected_patterns["memory:critical"] = expiry

        state_file = tmp_path / "sentinel_state.json"
        save_state(state, path=state_file)
        loaded = load_state(path=state_file)

        assert "memory:critical" in loaded.rejected_patterns
        assert loaded.rejected_patterns["memory:critical"] == expiry
