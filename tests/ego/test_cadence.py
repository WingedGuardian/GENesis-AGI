"""Tests for the ego cadence manager.

After PR 2 (unified cognitive loop), _on_tick() pushes signals to the
SignalQueue and _process_signals() drains the queue and calls
run_unified_cycle(). Tests that verify end-to-end proactive behavior
call _on_tick() then _process_signals().
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.db.schema import TABLES
from genesis.ego.cadence import _RECENCY_TIERS, EgoCadenceManager
from genesis.ego.session import CycleBlockedError
from genesis.ego.types import EgoConfig, EgoCycle

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        for table in ("ego_cycles", "ego_state", "cc_sessions"):
            await conn.execute(TABLES[table])
        yield conn


@pytest.fixture
def config():
    return EgoConfig(
        cadence_minutes=60,
        activity_threshold_minutes=30,
        max_interval_minutes=240,
        backoff_multiplier=2.0,
        consecutive_failure_limit=3,
        failure_backoff_minutes=60,
    )


@pytest.fixture
def mock_session():
    """Mock EgoSession with controllable run_unified_cycle."""
    session = AsyncMock()
    # All cycle types flow through run_unified_cycle via the signal consumer
    session.run_unified_cycle.return_value = EgoCycle(
        id="u1",
        output_text="unified test",
        proposals_json=json.dumps([{"action_type": "test"}]),
        focus_summary="unified testing",
        model_used="opus",
        cost_usd=0.15,
        ego_source="user_ego_cycle",
    )
    return session


@pytest.fixture
def mock_idle_detector():
    """IdleDetector that defaults to idle."""
    detector = MagicMock()
    detector.is_idle.return_value = True
    return detector


@pytest.fixture(autouse=True)
def _setup_complete_marker(tmp_path, monkeypatch):
    """Ensure the onboarding marker exists so ego gates pass by default.

    Tests that specifically check onboarding-incomplete behavior override
    Path.home() themselves.
    """
    genesis_dir = tmp_path / ".genesis"
    genesis_dir.mkdir()
    (genesis_dir / "setup-complete").write_text("2026-01-01")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture
def cadence(mock_session, config, mock_idle_detector, db):
    return EgoCadenceManager(
        session=mock_session,
        config=config,
        idle_detector=mock_idle_detector,
        db=db,
    )


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------


class TestCadenceLifecycle:
    async def test_start_registers_jobs(self, cadence):
        await cadence.start()
        jobs = cadence._scheduler.get_jobs()
        job_ids = {j.id for j in jobs}
        assert "ego_cycle" in job_ids
        assert "ego_morning_report" in job_ids
        # Consumer task should be running
        assert cadence._signal_consumer_task is not None
        assert not cadence._signal_consumer_task.done()
        await cadence.stop()

    async def test_stop_shuts_down(self, cadence):
        await cadence.start()
        assert cadence.is_running
        await cadence.stop()
        assert not cadence.is_running
        assert cadence._signal_consumer_task is None

    async def test_pause_resume(self, cadence):
        assert not cadence.is_paused
        cadence.pause()
        assert cadence.is_paused
        cadence.resume()
        assert not cadence.is_paused


# ---------------------------------------------------------------------------
# Tick behavior — _on_tick() now pushes signals, _process_signals() runs cycle
# ---------------------------------------------------------------------------


class TestCadenceTick:
    async def test_tick_pushes_signal_to_queue(
        self, cadence, mock_idle_detector,
    ):
        """_on_tick() pushes a signal to the queue (doesn't call run_cycle)."""
        mock_idle_detector.is_idle.return_value = True
        await cadence._on_tick()
        assert not cadence._signal_queue.empty()

    async def test_tick_runs_unified_cycle_when_idle(
        self, cadence, mock_session, mock_idle_detector,
    ):
        """_on_tick() + _process_signals() calls run_unified_cycle."""
        mock_idle_detector.is_idle.return_value = True
        await cadence._on_tick()
        await cadence._process_signals()
        mock_session.run_unified_cycle.assert_called_once()
        # model_override is None for normal proactive cycles
        assert mock_session.run_unified_cycle.call_args.kwargs.get("model_override") is None

    async def test_tick_skips_when_active(
        self, cadence, mock_session, mock_idle_detector,
    ):
        mock_idle_detector.is_idle.return_value = False
        await cadence._on_tick()
        mock_session.run_unified_cycle.assert_not_called()
        assert cadence._signal_queue.empty()

    async def test_tick_skips_when_onboarding_incomplete(
        self, cadence, mock_session, tmp_path,
    ):
        # Remove the marker created by autouse fixture
        (tmp_path / ".genesis" / "setup-complete").unlink()
        await cadence._on_tick()
        mock_session.run_unified_cycle.assert_not_called()
        assert cadence._signal_queue.empty()

    async def test_tick_skips_when_paused(
        self, cadence, mock_session,
    ):
        cadence.pause()
        await cadence._on_tick()
        mock_session.run_unified_cycle.assert_not_called()
        assert cadence._signal_queue.empty()

    async def test_tick_skips_when_circuit_open(
        self, cadence, mock_session,
    ):
        cadence._circuit_open_until = datetime.now(UTC) + timedelta(hours=1)
        await cadence._on_tick()
        mock_session.run_unified_cycle.assert_not_called()
        assert cadence._signal_queue.empty()

    async def test_tick_handles_exception(
        self, cadence, mock_session,
    ):
        """Exception in run_unified_cycle records a failure."""
        mock_session.run_unified_cycle.side_effect = RuntimeError("boom")
        await cadence._on_tick()
        await cadence._process_signals()
        assert cadence.consecutive_failures == 1

    async def test_tick_cycle_blocked_does_not_trip_breaker(
        self, cadence, mock_session,
    ):
        """CycleBlockedError is a gate, not a failure — no circuit breaker impact."""
        mock_session.run_unified_cycle.side_effect = CycleBlockedError("approval pending")
        await cadence._on_tick()
        await cadence._process_signals()
        assert cadence.consecutive_failures == 0

    async def test_morning_report_cycle_blocked_does_not_trip_breaker(
        self, cadence, mock_session,
    ):
        """CycleBlockedError in morning report also doesn't trip breaker."""
        mock_session.run_unified_cycle.side_effect = CycleBlockedError("approval pending")
        await cadence._on_morning_report()
        await cadence._process_signals()
        assert cadence.consecutive_failures == 0


# ---------------------------------------------------------------------------
# Signal consumer — _process_signals() tests
# ---------------------------------------------------------------------------


class TestProcessSignals:
    async def test_process_signals_calls_unified_cycle(
        self, cadence, mock_session,
    ):
        """_process_signals() drains queue and calls run_unified_cycle."""
        from genesis.ego.signals import EgoSignal

        cadence._signal_queue.push(EgoSignal(
            signal_type="timer",
            focus_category="proactive",
            summary="Idle tick #1",
        ))
        await cadence._process_signals()
        mock_session.run_unified_cycle.assert_called_once()
        # Signals are passed as first positional arg
        call_args = mock_session.run_unified_cycle.call_args
        signals = call_args[0][0]
        assert len(signals) == 1
        assert signals[0].summary == "Idle tick #1"

    async def test_process_signals_empty_queue_noop(
        self, cadence, mock_session,
    ):
        """Empty queue → no cycle call."""
        await cadence._process_signals()
        mock_session.run_unified_cycle.assert_not_called()

    async def test_deep_think_passes_model_override_via_metadata(
        self, cadence, mock_session, mock_idle_detector, monkeypatch,
    ):
        """Every Nth proactive cycle passes model_override='opus' via signal metadata."""
        mock_idle_detector.is_idle.return_value = True
        sonnet_config = EgoConfig(
            cadence_minutes=60, model="sonnet",  # Non-opus so deep-think triggers
        )
        cadence._config = sonnet_config
        # Prevent hot-reload from overwriting test config back to disk values
        monkeypatch.setattr(
            "genesis.ego.config.load_ego_config",
            lambda: sonnet_config,
        )
        cadence._deep_think_interval = 2  # Every 2nd cycle

        # Cycle 1: no override
        await cadence._on_tick()
        await cadence._process_signals()
        assert mock_session.run_unified_cycle.call_args.kwargs.get("model_override") is None

        # Cycle 2: deep-think → opus override
        mock_session.run_unified_cycle.reset_mock()
        await cadence._on_tick()
        await cadence._process_signals()
        assert mock_session.run_unified_cycle.call_args.kwargs.get("model_override") == "opus"

    async def test_signal_queue_created_on_init(self, cadence):
        """EgoCadenceManager creates a SignalQueue on init."""
        assert cadence._signal_queue is not None
        assert cadence._signal_queue.empty()

    async def test_consumer_loop_lifecycle(self, cadence):
        """start() creates consumer task, stop() cancels it."""
        assert cadence._signal_consumer_task is None
        await cadence.start()
        assert cadence._signal_consumer_task is not None
        task = cadence._signal_consumer_task
        assert not task.done()
        await cadence.stop()
        assert cadence._signal_consumer_task is None


# ---------------------------------------------------------------------------
# Morning report — now pushes signal instead of calling run_cycle
# ---------------------------------------------------------------------------


class TestMorningReport:
    async def test_morning_report_ignores_idle(
        self, cadence, mock_session, mock_idle_detector,
    ):
        """Morning report runs even when user is active."""
        mock_idle_detector.is_idle.return_value = False
        await cadence._on_morning_report()
        # Morning report now pushes a signal, not calls run_cycle
        assert not cadence._signal_queue.empty()
        signals = cadence._signal_queue.drain()
        assert len(signals) == 1
        assert signals[0].focus_category == "daily_briefing"

    async def test_morning_report_still_checks_pause(
        self, cadence, mock_session,
    ):
        cadence.pause()
        await cadence._on_morning_report()
        assert cadence._signal_queue.empty()

    async def test_morning_report_pushes_daily_briefing_signal(
        self, cadence,
    ):
        """Morning report signal has correct metadata."""
        await cadence._on_morning_report()
        signals = cadence._signal_queue.drain()
        assert len(signals) == 1
        sig = signals[0]
        assert sig.signal_type == "timer"
        assert sig.focus_category == "daily_briefing"
        assert sig.priority == "high"
        assert "Morning report" in sig.summary
        # No model_override — uses config model (user-configurable)
        assert "model_override" not in sig.metadata
        assert sig.metadata.get("effort_override") == "low"

    async def test_morning_report_effort_via_metadata(
        self, cadence, mock_session,
    ):
        """Morning report effort override flows through to unified cycle."""
        await cadence._on_morning_report()
        await cadence._process_signals()
        mock_session.run_unified_cycle.assert_called_once()
        call_kwargs = mock_session.run_unified_cycle.call_args[1]
        # model_override is None — uses config model, not hardcoded
        assert call_kwargs["model_override"] is None
        assert call_kwargs["effort_override"] == "low"

    async def test_morning_report_resets_interval(
        self, cadence, mock_session,
    ):
        """Morning report always resets interval to base (reporting event)."""
        # Back off the interval first
        cadence._current_interval = 120
        await cadence._on_morning_report()
        await cadence._process_signals()
        # Morning report should reset interval regardless of proposals
        assert cadence._current_interval == cadence._config.cadence_minutes


# ---------------------------------------------------------------------------
# Circuit breaker — driven via _on_tick() + _process_signals()
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    async def test_opens_after_n_failures(self, cadence, mock_session, config):
        mock_session.run_unified_cycle.return_value = None  # failure signal
        for _ in range(config.consecutive_failure_limit):
            await cadence._on_tick()
            await cadence._process_signals()

        assert cadence.consecutive_failures == config.consecutive_failure_limit
        assert cadence._circuit_open_until is not None

        # Next tick should be skipped due to circuit breaker
        mock_session.run_unified_cycle.reset_mock()
        await cadence._on_tick()
        mock_session.run_unified_cycle.assert_not_called()
        assert cadence._signal_queue.empty()

    async def test_resets_on_success(self, cadence, mock_session):
        # Simulate 2 failures
        mock_session.run_unified_cycle.return_value = None
        await cadence._on_tick()
        await cadence._process_signals()
        await cadence._on_tick()
        await cadence._process_signals()
        assert cadence.consecutive_failures == 2

        # Success resets
        mock_session.run_unified_cycle.return_value = EgoCycle(
            output_text="ok", proposals_json="[]", focus_summary="ok",
            ego_source="user_ego_cycle",
        )
        await cadence._on_tick()
        await cadence._process_signals()
        assert cadence.consecutive_failures == 0

    async def test_expires_after_backoff(self, cadence, config):
        cadence._consecutive_failures = config.consecutive_failure_limit
        cadence._circuit_open_until = datetime.now(UTC) - timedelta(seconds=1)

        # Circuit should be closed now (expired)
        assert not cadence._is_circuit_open()
        assert cadence._consecutive_failures == 0


# ---------------------------------------------------------------------------
# Adaptive interval — driven via _on_tick() + _process_signals()
# ---------------------------------------------------------------------------


class TestAdaptiveInterval:
    @pytest.fixture(autouse=True)
    def _isolate_config(self, monkeypatch, config):
        """Prevent hot-reload from overriding the test config with disk values."""
        monkeypatch.setattr(
            "genesis.ego.config.load_ego_config",
            lambda: config,
        )

    async def test_backoff_increases_interval(self, cadence, mock_session, config):
        # Cycle with no proposals → backoff
        mock_session.run_unified_cycle.return_value = EgoCycle(
            output_text="quiet", proposals_json="[]", focus_summary="idle",
            ego_source="user_ego_cycle",
        )
        await cadence._on_tick()
        await cadence._process_signals()
        assert cadence.current_interval_minutes == int(config.cadence_minutes * config.backoff_multiplier)

    async def test_proposals_reset_interval(self, cadence, mock_session, config):
        # First: backoff
        mock_session.run_unified_cycle.return_value = EgoCycle(
            output_text="quiet", proposals_json="[]", focus_summary="idle",
            ego_source="user_ego_cycle",
        )
        await cadence._on_tick()
        await cadence._process_signals()
        assert cadence.current_interval_minutes > config.cadence_minutes

        # Then: proposals reset
        mock_session.run_unified_cycle.return_value = EgoCycle(
            output_text="active",
            proposals_json=json.dumps([{"action_type": "test"}]),
            focus_summary="active",
            ego_source="user_ego_cycle",
        )
        await cadence._on_tick()
        await cadence._process_signals()
        assert cadence.current_interval_minutes == config.cadence_minutes

    async def test_backoff_capped_at_max(self, cadence, mock_session, config):
        """Multiple idle cycles don't exceed max_interval_minutes."""
        mock_session.run_unified_cycle.return_value = EgoCycle(
            output_text="quiet", proposals_json="[]", focus_summary="idle",
            ego_source="user_ego_cycle",
        )
        # Run enough times to exceed max
        for _ in range(10):
            await cadence._on_tick()
            await cadence._process_signals()
        assert cadence.current_interval_minutes <= config.max_interval_minutes


# ---------------------------------------------------------------------------
# User-recency tiers
# ---------------------------------------------------------------------------


async def _insert_foreground_session(
    db: aiosqlite.Connection, *, last_activity_at: str,
) -> None:
    """Insert a foreground session with the given last_activity_at."""
    await db.execute(
        "INSERT INTO cc_sessions "
        "(id, session_type, model, status, started_at, last_activity_at, source_tag) "
        "VALUES (?, 'foreground', 'opus', 'completed', ?, ?, 'foreground')",
        (f"sess_{last_activity_at}", last_activity_at, last_activity_at),
    )
    await db.commit()


class TestRecencyMaxInterval:
    """_recency_max_interval returns tier-appropriate max based on user activity."""

    async def test_recent_activity_returns_base_max(self, cadence, db):
        """Activity < 24h ago → 240 min max."""
        recent = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        await _insert_foreground_session(db, last_activity_at=recent)
        result = await cadence._recency_max_interval()
        assert result == 240

    async def test_1_to_3_days_returns_480(self, cadence, db):
        """Activity 1-3 days ago → 480 min max."""
        ts = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        await _insert_foreground_session(db, last_activity_at=ts)
        result = await cadence._recency_max_interval()
        assert result == 480

    async def test_3_to_7_days_returns_1440(self, cadence, db):
        """Activity 3-7 days ago → 1440 min max."""
        ts = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        await _insert_foreground_session(db, last_activity_at=ts)
        result = await cadence._recency_max_interval()
        assert result == 1440

    async def test_7_to_14_days_returns_2880(self, cadence, db):
        """Activity 7-14 days ago → 2880 min max."""
        ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        await _insert_foreground_session(db, last_activity_at=ts)
        result = await cadence._recency_max_interval()
        assert result == 2880

    async def test_14_plus_days_returns_4320(self, cadence, db):
        """Activity > 14 days ago → 4320 min max."""
        ts = (datetime.now(UTC) - timedelta(days=20)).isoformat()
        await _insert_foreground_session(db, last_activity_at=ts)
        result = await cadence._recency_max_interval()
        assert result == 4320

    async def test_no_foreground_sessions_returns_config_max(self, cadence):
        """No foreground sessions → fallback to config max."""
        result = await cadence._recency_max_interval()
        assert result == cadence._config.max_interval_minutes

    async def test_uses_most_recent_session(self, cadence, db):
        """Multiple sessions → uses most recent."""
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        recent = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
        await _insert_foreground_session(db, last_activity_at=old)
        await _insert_foreground_session(db, last_activity_at=recent)
        result = await cadence._recency_max_interval()
        assert result == 240  # <24h tier

    async def test_ignores_background_sessions(self, cadence, db):
        """Background sessions don't count as user presence."""
        recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        await db.execute(
            "INSERT INTO cc_sessions "
            "(id, session_type, model, status, started_at, last_activity_at, source_tag) "
            "VALUES ('bg1', 'background_reflection', 'sonnet', 'completed', ?, ?, 'reflection')",
            (recent, recent),
        )
        await db.commit()
        result = await cadence._recency_max_interval()
        assert result == cadence._config.max_interval_minutes  # no foreground → fallback


class TestRecencyAwareBackoff:
    """_update_interval respects recency-adjusted max."""

    @pytest.fixture(autouse=True)
    def _isolate_config(self, monkeypatch, config):
        monkeypatch.setattr(
            "genesis.ego.config.load_ego_config",
            lambda: config,
        )

    async def test_backoff_respects_recency_tier(self, cadence, mock_session, db):
        """When user was active 2 days ago, backoff caps at 480 (not 240)."""
        ts = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        await _insert_foreground_session(db, last_activity_at=ts)

        mock_session.run_unified_cycle.return_value = EgoCycle(
            output_text="quiet", proposals_json="[]", focus_summary="idle",
            ego_source="user_ego_cycle",
        )
        # Run enough idle cycles to hit the cap
        for _ in range(10):
            await cadence._on_tick()
            await cadence._process_signals()

        assert cadence.current_interval_minutes <= 480
        # Should have backed off beyond the old 240 cap
        assert cadence.current_interval_minutes > 240

    async def test_user_return_resets_to_base(self, cadence, mock_session, db):
        """After a productive cycle, interval resets to base regardless of tier."""
        # Start in a high tier
        old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        await _insert_foreground_session(db, last_activity_at=old_ts)

        mock_session.run_unified_cycle.return_value = EgoCycle(
            output_text="quiet", proposals_json="[]", focus_summary="idle",
            ego_source="user_ego_cycle",
        )
        for _ in range(5):
            await cadence._on_tick()
            await cadence._process_signals()
        assert cadence.current_interval_minutes > cadence._config.cadence_minutes

        # Productive cycle → reset to base
        mock_session.run_unified_cycle.return_value = EgoCycle(
            output_text="active",
            proposals_json=json.dumps([{"action_type": "test"}]),
            focus_summary="active",
            ego_source="user_ego_cycle",
        )
        await cadence._on_tick()
        await cadence._process_signals()
        assert cadence.current_interval_minutes == cadence._config.cadence_minutes


class TestRecencyTiersConstant:
    """Validate the _RECENCY_TIERS constant is well-formed."""

    def test_tiers_ordered(self):
        """Thresholds must be in ascending order, with None last."""
        for i in range(len(_RECENCY_TIERS) - 1):
            threshold, _ = _RECENCY_TIERS[i]
            assert threshold is not None, "Only the last tier should have None threshold"
            if i + 1 < len(_RECENCY_TIERS) - 1:
                next_threshold, _ = _RECENCY_TIERS[i + 1]
                assert next_threshold is not None
                assert threshold < next_threshold

    def test_last_tier_is_catchall(self):
        """Last tier must have None threshold (catchall)."""
        assert _RECENCY_TIERS[-1][0] is None

    def test_max_intervals_increase(self):
        """Max intervals should increase with each tier."""
        for i in range(len(_RECENCY_TIERS) - 1):
            _, max_a = _RECENCY_TIERS[i]
            _, max_b = _RECENCY_TIERS[i + 1]
            assert max_b > max_a


# ---------------------------------------------------------------------------
# Reactive signals — push_reactive_event → signal queue → unified cycle
# ---------------------------------------------------------------------------


class TestReactiveSignals:
    def test_push_reactive_creates_signal(self, cadence):
        """push_reactive_event creates an EgoSignal in the signal queue."""
        cadence._running = True
        cadence.push_reactive_event({
            "type": "breaker.tripped",
            "summary": "Provider X circuit breaker tripped",
            "priority": "high",
            "source": "routing",
        })
        assert not cadence._signal_queue.empty()
        signals = cadence._signal_queue.drain()
        assert len(signals) == 1
        sig = signals[0]
        assert sig.signal_type == "event"
        assert sig.focus_category == "reactive"
        assert "Provider X" in sig.summary
        assert sig.metadata["model_override"] == "opus"
        assert sig.metadata["effort_override"] == "high"
        assert sig.metadata["event_type"] == "breaker.tripped"
        assert sig.metadata["source"] == "routing"

    def test_push_reactive_dedup_via_signal_queue(self, cadence):
        """Same summary rejected by SignalQueue's 6h dedup."""
        cadence._running = True
        event = {
            "type": "test",
            "summary": "Same event summary",
            "priority": "high",
        }
        cadence.push_reactive_event(event)
        cadence.push_reactive_event(event)  # Should be deduped
        signals = cadence._signal_queue.drain()
        assert len(signals) == 1

    def test_push_reactive_skips_when_paused(self, cadence):
        """No signal pushed when paused."""
        cadence._running = True
        cadence.pause()
        cadence.push_reactive_event({"type": "test", "summary": "x"})
        assert cadence._signal_queue.empty()

    def test_push_reactive_skips_when_not_running(self, cadence):
        """No signal pushed when not running."""
        cadence._running = False
        cadence.push_reactive_event({"type": "test", "summary": "x"})
        assert cadence._signal_queue.empty()

    def test_push_reactive_model_override_is_opus(self, cadence):
        """Reactive signals carry opus/high to match CYCLE_TYPE_DEFAULTS[REACTIVE]."""
        cadence._running = True
        cadence.push_reactive_event({"type": "test", "summary": "alert"})
        signals = cadence._signal_queue.drain()
        assert signals[0].metadata["model_override"] == "opus"
        assert signals[0].metadata["effort_override"] == "high"

    async def test_reactive_flows_through_unified_cycle(
        self, cadence, mock_session,
    ):
        """push_reactive_event + _process_signals → run_unified_cycle."""
        cadence._running = True
        cadence.push_reactive_event({
            "type": "health.degraded",
            "summary": "System health degraded",
            "priority": "high",
        })
        await cadence._process_signals()
        mock_session.run_unified_cycle.assert_called_once()
        call_args = mock_session.run_unified_cycle.call_args
        signals = call_args[0][0]
        assert len(signals) == 1
        assert signals[0].focus_category == "reactive"
        assert call_args[1]["model_override"] == "opus"
        assert call_args[1]["effort_override"] == "high"

    async def test_reactive_rate_limit_at_consumer(
        self, cadence, mock_session,
    ):
        """Consumer drops reactive signals when rate limit (3/hour) is hit."""
        cadence._running = True
        # Simulate 3 prior reactive cycles this hour
        now = datetime.now(UTC)
        cadence._reactive_timestamps = [
            now - timedelta(minutes=10),
            now - timedelta(minutes=20),
            now - timedelta(minutes=30),
        ]
        cadence.push_reactive_event({
            "type": "test",
            "summary": "4th event this hour",
            "priority": "high",
        })
        await cadence._process_signals()
        mock_session.run_unified_cycle.assert_not_called()

    async def test_reactive_rate_limit_preserves_non_reactive(
        self, cadence, mock_session,
    ):
        """Non-reactive signals survive when reactive rate limit is hit."""
        cadence._running = True
        # Saturate reactive rate limit
        now = datetime.now(UTC)
        cadence._reactive_timestamps = [
            now - timedelta(minutes=10),
            now - timedelta(minutes=20),
            now - timedelta(minutes=30),
        ]
        # Push a reactive signal AND a proactive signal
        from genesis.ego.signals import EgoSignal

        cadence._signal_queue.push(EgoSignal(
            signal_type="event",
            focus_category="reactive",
            summary="reactive event",
            priority="high",
        ))
        cadence._signal_queue.push(EgoSignal(
            signal_type="timer",
            focus_category="proactive",
            summary="proactive tick",
            priority="medium",
        ))
        await cadence._process_signals()
        # Should still run with the proactive signal
        mock_session.run_unified_cycle.assert_called_once()
        call_args = mock_session.run_unified_cycle.call_args
        signals = call_args[0][0]
        assert len(signals) == 1
        assert signals[0].focus_category == "proactive"

    async def test_reactive_records_timestamp_on_success(
        self, cadence, mock_session,
    ):
        """Successful reactive cycle records a timestamp for rate limiting."""
        cadence._running = True
        assert len(cadence._reactive_timestamps) == 0
        cadence.push_reactive_event({
            "type": "test",
            "summary": "alert event",
            "priority": "high",
        })
        await cadence._process_signals()
        assert len(cadence._reactive_timestamps) == 1

    def test_push_reactive_priority_mapping(self, cadence):
        """Event priority strings map to signal priority levels."""
        from genesis.ego.cadence import _map_priority

        assert _map_priority("CRITICAL") == "critical"
        assert _map_priority("ERROR") == "high"
        assert _map_priority("WARNING") == "medium"
        assert _map_priority("high") == "high"
        assert _map_priority("medium") == "medium"
        assert _map_priority("low") == "low"
        assert _map_priority("unknown") == "low"


# ---------------------------------------------------------------------------
# Escalation signals — push_escalation_event → signal queue → unified cycle
# ---------------------------------------------------------------------------


class TestEscalationSignals:
    def test_push_escalation_creates_signal(self, cadence):
        """push_escalation_event creates a critical escalation signal."""
        cadence._running = True
        cadence.push_escalation_event({
            "type": "health.critical",
            "summary": "Database unreachable",
            "priority": "CRITICAL",
            "source": "health",
        })
        assert not cadence._signal_queue.empty()
        signals = cadence._signal_queue.drain()
        assert len(signals) == 1
        sig = signals[0]
        assert sig.signal_type == "event"
        assert sig.focus_category == "escalation"
        assert sig.priority == "critical"
        assert sig.metadata["model_override"] == "sonnet"
        assert sig.metadata["effort_override"] == "medium"

    async def test_escalation_not_rate_limited(
        self, cadence, mock_session,
    ):
        """Escalation signals are NOT subject to reactive rate limiting."""
        cadence._running = True
        # Saturate reactive rate limit
        now = datetime.now(UTC)
        cadence._reactive_timestamps = [
            now - timedelta(minutes=10),
            now - timedelta(minutes=20),
            now - timedelta(minutes=30),
        ]
        cadence.push_escalation_event({
            "type": "health.critical",
            "summary": "Critical system failure",
        })
        await cadence._process_signals()
        # Should still run — escalation is not reactive
        mock_session.run_unified_cycle.assert_called_once()
        call_args = mock_session.run_unified_cycle.call_args
        signals = call_args[0][0]
        assert signals[0].focus_category == "escalation"

    def test_push_escalation_skips_when_paused(self, cadence):
        """No signal pushed when paused."""
        cadence._running = True
        cadence.pause()
        cadence.push_escalation_event({"type": "test", "summary": "x"})
        assert cadence._signal_queue.empty()

    async def test_escalation_survives_reactive_rate_limit_in_mixed_batch(
        self, cadence, mock_session,
    ):
        """Escalation survives reactive rate limit when batched with reactive signal."""
        cadence._running = True
        now = datetime.now(UTC)
        cadence._reactive_timestamps = [
            now - timedelta(minutes=10),
            now - timedelta(minutes=20),
            now - timedelta(minutes=30),
        ]
        # Push both reactive and escalation in the same batch
        cadence.push_reactive_event({"type": "test", "summary": "rate limited reactive"})
        cadence.push_escalation_event({"type": "health.critical", "summary": "critical failure"})
        await cadence._process_signals()
        # Escalation must survive; reactive must be dropped
        mock_session.run_unified_cycle.assert_called_once()
        call_args = mock_session.run_unified_cycle.call_args
        signals = call_args[0][0]
        assert len(signals) == 1
        assert signals[0].focus_category == "escalation"

    async def test_escalation_flows_through_unified_cycle(
        self, cadence, mock_session,
    ):
        """push_escalation_event + _process_signals → run_unified_cycle."""
        cadence._running = True
        cadence.push_escalation_event({
            "type": "guardian.alert",
            "summary": "Container OOM detected",
        })
        await cadence._process_signals()
        mock_session.run_unified_cycle.assert_called_once()
        call_args = mock_session.run_unified_cycle.call_args
        assert call_args[1]["model_override"] == "sonnet"
        assert call_args[1]["effort_override"] == "medium"
