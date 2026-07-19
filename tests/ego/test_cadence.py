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
        # Neutral by default — quiet-hours behavior is exercised explicitly in
        # TestQuietHours, so shared tests stay deterministic overnight.
        quiet_hours_enabled=False,
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
        # Dedicated liveness pulse, decoupled from the proactive _on_tick.
        assert "ego_heartbeat" in job_ids
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
# Restart-safe boot first-fire (B1): anchor ego_cycle to job_health.last_success
# ---------------------------------------------------------------------------

_JOB_HEALTH_DDL = """
    CREATE TABLE IF NOT EXISTS job_health (
        job_name         TEXT PRIMARY KEY,
        last_run         TEXT,
        last_success     TEXT,
        last_failure     TEXT,
        last_error       TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        total_runs       INTEGER NOT NULL DEFAULT 0,
        total_successes  INTEGER NOT NULL DEFAULT 0,
        total_failures   INTEGER NOT NULL DEFAULT 0,
        updated_at       TEXT NOT NULL
    )
"""


async def _seed_last_success(conn, job_name: str, last_success: datetime) -> None:
    """Create job_health (not in TABLES) and seed one ego's last_success row."""
    await conn.execute(_JOB_HEALTH_DDL)
    iso = last_success.isoformat()
    await conn.execute(
        "INSERT INTO job_health "
        "(job_name, last_run, last_success, consecutive_failures, "
        " total_runs, total_successes, total_failures, updated_at) "
        "VALUES (?, ?, ?, 0, 1, 1, 0, ?)",
        (job_name, iso, iso, iso),
    )
    await conn.commit()


class TestBootFirstFire:
    """The ego_cycle boot first-fire is anchored to this ego's OWN persisted
    job_health.last_success so a restart cannot starve the proactive cycle
    (the IntervalTrigger-resets-on-restart trap)."""

    async def test_overdue_fires_soon(self, cadence, mock_session, db):
        mock_session._source_tag = "user_ego_cycle"
        await _seed_last_success(
            db,
            "user_ego_cycle",
            datetime.now(UTC) - timedelta(days=5),
        )
        first_fire = await cadence._compute_boot_first_fire()
        assert first_fire is not None
        delta = (first_fire - datetime.now(UTC)).total_seconds()
        # overdue by >> base → ~60s boot-pin, not a full base interval away
        assert 0 < delta <= 120

    async def test_recent_waits_remaining_base(self, cadence, mock_session, db):
        mock_session._source_tag = "user_ego_cycle"
        last = datetime.now(UTC) - timedelta(minutes=10)  # base=60 → +50m ahead
        await _seed_last_success(db, "user_ego_cycle", last)
        first_fire = await cadence._compute_boot_first_fire()
        expected = last + timedelta(minutes=60)  # config.cadence_minutes == 60
        assert abs((first_fire - expected).total_seconds()) < 2

    async def test_no_row_returns_none(self, cadence, mock_session, db):
        mock_session._source_tag = "genesis_ego_cycle"
        await db.execute(_JOB_HEALTH_DDL)  # table exists, no row for this ego
        await db.commit()
        assert await cadence._compute_boot_first_fire() is None

    async def test_missing_table_returns_none(self, cadence, mock_session):
        # job_health never created → read raises → swallowed → None (fresh path)
        mock_session._source_tag = "genesis_ego_cycle"
        assert await cadence._compute_boot_first_fire() is None

    async def test_per_ego_isolation(self, cadence, mock_session, db):
        # only the genesis row exists; the user ego must NOT read it
        await _seed_last_success(
            db,
            "genesis_ego_cycle",
            datetime.now(UTC) - timedelta(days=5),
        )
        mock_session._source_tag = "user_ego_cycle"
        assert await cadence._compute_boot_first_fire() is None

    async def test_start_pins_next_run_time_when_overdue(
        self,
        cadence,
        mock_session,
        db,
    ):
        mock_session._source_tag = "user_ego_cycle"
        await _seed_last_success(
            db,
            "user_ego_cycle",
            datetime.now(UTC) - timedelta(days=5),
        )
        await cadence.start()
        try:
            job = cadence._scheduler.get_job("ego_cycle")
            assert job is not None and job.next_run_time is not None
            delta = (job.next_run_time - datetime.now(UTC)).total_seconds()
            assert 0 < delta <= 120
        finally:
            await cadence.stop()

    async def test_start_fresh_install_not_paused(self, cadence, mock_session):
        # No job_health row → next_run_time OMITTED → trigger computes it
        # (now + base). Must NOT be paused: an explicit None would pause it.
        mock_session._source_tag = "genesis_ego_cycle"
        await cadence.start()
        try:
            job = cadence._scheduler.get_job("ego_cycle")
            assert job is not None
            assert job.next_run_time is not None  # not paused
        finally:
            await cadence.stop()


# ---------------------------------------------------------------------------
# Heartbeat — dedicated fixed-interval liveness pulse (decoupled from _on_tick)
# ---------------------------------------------------------------------------


class TestCadenceHeartbeat:
    async def test_on_heartbeat_emits_alive(self, cadence):
        """The dedicated heartbeat job emits an 'alive' liveness pulse."""
        cadence._emit_heartbeat = MagicMock()
        await cadence._on_heartbeat()
        cadence._emit_heartbeat.assert_called_once_with("alive")

    async def test_on_heartbeat_emits_even_when_paused(self, cadence):
        """Liveness pulse fires regardless of pause — a paused ego is alive,
        not dead, and must not trigger a false 'overdue' health alarm."""
        cadence.pause()
        cadence._emit_heartbeat = MagicMock()
        await cadence._on_heartbeat()
        cadence._emit_heartbeat.assert_called_once_with("alive")

    async def test_emit_heartbeat_reaches_event_bus_with_ego_contract(self, cadence):
        """_emit_heartbeat fires a Subsystem.EGO 'heartbeat' event — the exact
        (subsystem, event_type) contract the health read-path queries on."""
        from unittest.mock import patch

        from genesis.observability.types import Severity, Subsystem

        bus = MagicMock()
        bus.emit = AsyncMock()
        cadence._event_bus = bus

        # _emit_heartbeat builds the emit() coroutine synchronously (recording
        # the call) then hands it to tracked_task. Stub tracked_task so no loose
        # task is left pending, and close the captured coroutine cleanly.
        # Patch the SOURCE module (genesis.util.tasks): _emit_heartbeat imports
        # tracked_task inline (function-local) and re-resolves it from that
        # module on each call, so there is no genesis.ego.cadence.tracked_task
        # module-scope binding to target.
        def _fake_tracked_task(coro, **kwargs):
            coro.close()
            return None

        with patch("genesis.util.tasks.tracked_task", side_effect=_fake_tracked_task):
            cadence._emit_heartbeat("alive")

        bus.emit.assert_called_once()
        args = bus.emit.call_args.args
        assert (args[0], args[1], args[2]) == (
            Subsystem.EGO,
            Severity.DEBUG,
            "heartbeat",
        )
        assert args[3].startswith("ego_alive")

    async def test_emit_heartbeat_noop_without_event_bus(self, cadence):
        """No event bus (e.g. standalone/test) → silent no-op, never raises."""
        assert cadence._event_bus is None
        cadence._emit_heartbeat("alive")  # must not raise

    async def test_start_emits_initial_heartbeat(self, cadence):
        """start() fires an immediate liveness pulse so a fresh restart is not
        reported stale until the first 5-min ego_heartbeat firing."""
        cadence._emit_heartbeat = MagicMock()
        await cadence.start()
        try:
            cadence._emit_heartbeat.assert_called_once_with("start")
        finally:
            await cadence.stop()


# ---------------------------------------------------------------------------
# Tick behavior — _on_tick() now pushes signals, _process_signals() runs cycle
# ---------------------------------------------------------------------------


class TestCadenceTick:
    async def test_tick_pushes_signal_to_queue(
        self,
        cadence,
        mock_idle_detector,
    ):
        """_on_tick() pushes a signal to the queue (doesn't call run_cycle)."""
        mock_idle_detector.is_idle.return_value = True
        await cadence._on_tick()
        assert not cadence._signal_queue.empty()

    async def test_tick_runs_unified_cycle_when_idle(
        self,
        cadence,
        mock_session,
        mock_idle_detector,
    ):
        """_on_tick() + _process_signals() calls run_unified_cycle."""
        mock_idle_detector.is_idle.return_value = True
        await cadence._on_tick()
        await cadence._process_signals()
        mock_session.run_unified_cycle.assert_called_once()
        # model_override is None for normal proactive cycles
        assert mock_session.run_unified_cycle.call_args.kwargs.get("model_override") is None

    async def test_tick_skips_when_active(
        self,
        cadence,
        mock_session,
        mock_idle_detector,
    ):
        mock_idle_detector.is_idle.return_value = False
        await cadence._on_tick()
        mock_session.run_unified_cycle.assert_not_called()
        assert cadence._signal_queue.empty()

    async def test_tick_skips_when_onboarding_incomplete(
        self,
        cadence,
        mock_session,
        tmp_path,
    ):
        # Remove the marker created by autouse fixture
        (tmp_path / ".genesis" / "setup-complete").unlink()
        await cadence._on_tick()
        mock_session.run_unified_cycle.assert_not_called()
        assert cadence._signal_queue.empty()

    async def test_tick_skips_when_paused(
        self,
        cadence,
        mock_session,
    ):
        cadence.pause()
        await cadence._on_tick()
        mock_session.run_unified_cycle.assert_not_called()
        assert cadence._signal_queue.empty()

    async def test_tick_skips_when_circuit_open(
        self,
        cadence,
        mock_session,
    ):
        cadence._circuit_open_until = datetime.now(UTC) + timedelta(hours=1)
        await cadence._on_tick()
        mock_session.run_unified_cycle.assert_not_called()
        assert cadence._signal_queue.empty()

    async def test_tick_handles_exception(
        self,
        cadence,
        mock_session,
    ):
        """Exception in run_unified_cycle records a failure."""
        mock_session.run_unified_cycle.side_effect = RuntimeError("boom")
        await cadence._on_tick()
        await cadence._process_signals()
        assert cadence.consecutive_failures == 1

    async def test_tick_cycle_blocked_does_not_trip_breaker(
        self,
        cadence,
        mock_session,
    ):
        """CycleBlockedError is a gate, not a failure — no circuit breaker impact."""
        mock_session.run_unified_cycle.side_effect = CycleBlockedError("approval pending")
        await cadence._on_tick()
        await cadence._process_signals()
        assert cadence.consecutive_failures == 0

    async def test_morning_report_cycle_blocked_does_not_trip_breaker(
        self,
        cadence,
        mock_session,
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
        self,
        cadence,
        mock_session,
    ):
        """_process_signals() drains queue and calls run_unified_cycle."""
        from genesis.ego.signals import EgoSignal

        cadence._signal_queue.push(
            EgoSignal(
                signal_type="timer",
                focus_category="proactive",
                summary="Idle tick #1",
            )
        )
        await cadence._process_signals()
        mock_session.run_unified_cycle.assert_called_once()
        # Signals are passed as first positional arg
        call_args = mock_session.run_unified_cycle.call_args
        signals = call_args[0][0]
        assert len(signals) == 1
        assert signals[0].summary == "Idle tick #1"

    async def test_process_signals_empty_queue_noop(
        self,
        cadence,
        mock_session,
    ):
        """Empty queue → no cycle call."""
        await cadence._process_signals()
        mock_session.run_unified_cycle.assert_not_called()

    async def test_deep_think_passes_model_override_via_metadata(
        self,
        cadence,
        mock_session,
        mock_idle_detector,
        monkeypatch,
    ):
        """Every Nth COMPLETED proactive cycle runs on Opus (decided at consume
        time in _process_signals, not at tick time)."""
        mock_idle_detector.is_idle.return_value = True
        sonnet_config = EgoConfig(
            cadence_minutes=60,
            model="sonnet",  # Non-opus so deep-think triggers
            quiet_hours_enabled=False,  # keep this test independent of wall clock
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
        self,
        cadence,
        mock_session,
        mock_idle_detector,
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
        self,
        cadence,
        mock_session,
    ):
        cadence.pause()
        await cadence._on_morning_report()
        assert cadence._signal_queue.empty()

    async def test_morning_report_pushes_daily_briefing_signal(
        self,
        cadence,
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
        self,
        cadence,
        mock_session,
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
        self,
        cadence,
        mock_session,
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
            output_text="ok",
            proposals_json="[]",
            focus_summary="ok",
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
            output_text="quiet",
            proposals_json="[]",
            focus_summary="idle",
            ego_source="user_ego_cycle",
        )
        await cadence._on_tick()
        await cadence._process_signals()
        assert cadence.current_interval_minutes == int(
            config.cadence_minutes * config.backoff_multiplier
        )

    async def test_proposals_reset_interval(self, cadence, mock_session, config):
        # First: backoff
        mock_session.run_unified_cycle.return_value = EgoCycle(
            output_text="quiet",
            proposals_json="[]",
            focus_summary="idle",
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
            output_text="quiet",
            proposals_json="[]",
            focus_summary="idle",
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
    db: aiosqlite.Connection,
    *,
    last_activity_at: str,
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
            output_text="quiet",
            proposals_json="[]",
            focus_summary="idle",
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
            output_text="quiet",
            proposals_json="[]",
            focus_summary="idle",
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
        cadence.push_reactive_event(
            {
                "type": "breaker.tripped",
                "summary": "Provider X circuit breaker tripped",
                "priority": "high",
                "source": "routing",
            }
        )
        assert not cadence._signal_queue.empty()
        signals = cadence._signal_queue.drain()
        assert len(signals) == 1
        sig = signals[0]
        assert sig.signal_type == "event"
        assert sig.focus_category == "reactive"
        assert "Provider X" in sig.summary
        # No forced overrides — each ego's base config rules.
        assert "model_override" not in sig.metadata
        assert "effort_override" not in sig.metadata
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

    def test_push_reactive_has_no_model_or_effort_override(self, cadence):
        """Reactive signals carry NO overrides — each ego's base config rules."""
        cadence._running = True
        cadence.push_reactive_event({"type": "test", "summary": "alert"})
        signals = cadence._signal_queue.drain()
        assert "model_override" not in signals[0].metadata
        assert "effort_override" not in signals[0].metadata

    async def test_reactive_flows_through_unified_cycle(
        self,
        cadence,
        mock_session,
    ):
        """push_reactive_event + _process_signals → run_unified_cycle."""
        cadence._running = True
        cadence.push_reactive_event(
            {
                "type": "health.degraded",
                "summary": "System health degraded",
                "priority": "high",
            }
        )
        await cadence._process_signals()
        mock_session.run_unified_cycle.assert_called_once()
        call_args = mock_session.run_unified_cycle.call_args
        signals = call_args[0][0]
        assert len(signals) == 1
        assert signals[0].focus_category == "reactive"
        # No overrides forwarded — the ego runs on its base config.
        assert call_args[1]["model_override"] is None
        assert call_args[1]["effort_override"] is None

    async def test_reactive_rate_limit_at_consumer(
        self,
        cadence,
        mock_session,
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
        cadence.push_reactive_event(
            {
                "type": "test",
                "summary": "4th event this hour",
                "priority": "high",
            }
        )
        await cadence._process_signals()
        mock_session.run_unified_cycle.assert_not_called()

    async def test_reactive_rate_limit_preserves_non_reactive(
        self,
        cadence,
        mock_session,
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

        cadence._signal_queue.push(
            EgoSignal(
                signal_type="event",
                focus_category="reactive",
                summary="reactive event",
                priority="high",
            )
        )
        cadence._signal_queue.push(
            EgoSignal(
                signal_type="timer",
                focus_category="proactive",
                summary="proactive tick",
                priority="medium",
            )
        )
        await cadence._process_signals()
        # Should still run with the proactive signal
        mock_session.run_unified_cycle.assert_called_once()
        call_args = mock_session.run_unified_cycle.call_args
        signals = call_args[0][0]
        assert len(signals) == 1
        assert signals[0].focus_category == "proactive"

    async def test_reactive_records_timestamp_on_success(
        self,
        cadence,
        mock_session,
    ):
        """Successful reactive cycle records a timestamp for rate limiting."""
        cadence._running = True
        assert len(cadence._reactive_timestamps) == 0
        cadence.push_reactive_event(
            {
                "type": "test",
                "summary": "alert event",
                "priority": "high",
            }
        )
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
        cadence.push_escalation_event(
            {
                "type": "health.critical",
                "summary": "Database unreachable",
                "priority": "CRITICAL",
                "source": "health",
            }
        )
        assert not cadence._signal_queue.empty()
        signals = cadence._signal_queue.drain()
        assert len(signals) == 1
        sig = signals[0]
        assert sig.signal_type == "event"
        assert sig.focus_category == "escalation"
        assert sig.priority == "critical"
        # No model override (base config rules); effort forced to high —
        # never think less about a CRITICAL escalation than a routine tick.
        assert "model_override" not in sig.metadata
        assert sig.metadata["effort_override"] == "high"

    async def test_escalation_not_rate_limited(
        self,
        cadence,
        mock_session,
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
        cadence.push_escalation_event(
            {
                "type": "health.critical",
                "summary": "Critical system failure",
            }
        )
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
        self,
        cadence,
        mock_session,
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
        self,
        cadence,
        mock_session,
    ):
        """push_escalation_event + _process_signals → run_unified_cycle."""
        cadence._running = True
        cadence.push_escalation_event(
            {
                "type": "guardian.alert",
                "summary": "Container OOM detected",
            }
        )
        await cadence._process_signals()
        mock_session.run_unified_cycle.assert_called_once()
        call_args = mock_session.run_unified_cycle.call_args
        assert call_args[1]["model_override"] is None
        assert call_args[1]["effort_override"] == "high"


# ---------------------------------------------------------------------------
# Goal staleness scanner
# ---------------------------------------------------------------------------


class TestGoalStaleness:
    """Tests for _check_stale_goals() — goal_review signal collector."""

    @pytest.fixture
    async def goal_db(self):
        """DB with user_goals + ego_proposals tables (proposals for stuck-detection)."""
        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            for table in (
                "ego_cycles",
                "ego_state",
                "cc_sessions",
                "user_goals",
                "ego_proposals",
            ):
                await conn.execute(TABLES[table])
            await conn.commit()
            yield conn

    @pytest.fixture
    def goal_cadence(self, mock_session, config, mock_idle_detector, goal_db):
        mock_session._source_tag = "user_ego_cycle"
        mock_session._db = goal_db
        return EgoCadenceManager(
            session=mock_session,
            config=config,
            idle_detector=mock_idle_detector,
            db=goal_db,
        )

    async def test_stale_goal_pushes_signal(self, goal_cadence, goal_db):
        """Goal stale beyond threshold pushes a goal_review signal."""
        twenty_days_ago = (datetime.now(UTC) - timedelta(days=20)).isoformat()
        await goal_db.execute(
            "INSERT INTO user_goals "
            "(id, title, category, priority, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("goal-1", "Test Goal", "project", "high", "active", twenty_days_ago, twenty_days_ago),
        )
        await goal_db.commit()

        await goal_cadence._check_stale_goals()

        assert not goal_cadence._signal_queue.empty()
        signals = goal_cadence._signal_queue.drain()
        assert len(signals) == 1
        sig = signals[0]
        assert sig.focus_category == "goal_review"
        assert sig.focus_id == "goal-1"
        assert "20d" in sig.summary

    async def test_fresh_goal_no_signal(self, goal_cadence, goal_db):
        """Goal updated recently should NOT trigger a signal."""
        now = datetime.now(UTC).isoformat()
        await goal_db.execute(
            "INSERT INTO user_goals "
            "(id, title, category, priority, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("goal-2", "Fresh Goal", "project", "medium", "active", now, now),
        )
        await goal_db.commit()

        await goal_cadence._check_stale_goals()
        assert goal_cadence._signal_queue.empty()

    async def test_genesis_ego_skipped(self, goal_cadence, goal_db):
        """Genesis ego should NOT run goal staleness checks."""
        goal_cadence._session._source_tag = "genesis_ego_cycle"
        twenty_days_ago = (datetime.now(UTC) - timedelta(days=20)).isoformat()
        await goal_db.execute(
            "INSERT INTO user_goals "
            "(id, title, category, priority, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("goal-3", "Stale Goal", "career", "high", "active", twenty_days_ago, twenty_days_ago),
        )
        await goal_db.commit()

        await goal_cadence._check_stale_goals()
        assert goal_cadence._signal_queue.empty()

    async def test_respects_config_threshold(self, goal_cadence, goal_db):
        """Uses goal_review_staleness_days from config."""
        # Set threshold to 30 days, goal is only 15 days old
        goal_cadence._config.goal_review_staleness_days = 30
        fifteen_days_ago = (datetime.now(UTC) - timedelta(days=15)).isoformat()
        await goal_db.execute(
            "INSERT INTO user_goals "
            "(id, title, category, priority, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "goal-4",
                "Not Stale Yet",
                "learning",
                "medium",
                "active",
                fifteen_days_ago,
                fifteen_days_ago,
            ),
        )
        await goal_db.commit()

        await goal_cadence._check_stale_goals()
        assert goal_cadence._signal_queue.empty()

        # Now set threshold to 10 — same goal should trigger
        goal_cadence._config.goal_review_staleness_days = 10
        await goal_cadence._check_stale_goals()
        assert not goal_cadence._signal_queue.empty()

    async def test_per_goal_cadence_days(self, goal_cadence, goal_db):
        """Goal with cadence_days=7 triggers at 8 days stale."""
        goal_cadence._config.goal_review_staleness_days = 30  # global: 30d
        eight_days_ago = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        await goal_db.execute(
            "INSERT INTO user_goals "
            "(id, title, category, priority, status, created_at, updated_at, "
            " goal_type, cadence_days) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "goal-cad1",
                "Weekly Review Goal",
                "project",
                "high",
                "active",
                eight_days_ago,
                eight_days_ago,
                "milestone",
                7,
            ),
        )
        await goal_db.commit()

        await goal_cadence._check_stale_goals()
        # Should trigger: 8 days > cadence_days=7, despite global=30
        assert not goal_cadence._signal_queue.empty()

    async def test_per_goal_cadence_falls_back(self, goal_cadence, goal_db):
        """Goal without cadence_days uses global threshold."""
        goal_cadence._config.goal_review_staleness_days = 30
        eight_days_ago = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        await goal_db.execute(
            "INSERT INTO user_goals "
            "(id, title, category, priority, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "goal-cad2",
                "No Cadence Goal",
                "project",
                "medium",
                "active",
                eight_days_ago,
                eight_days_ago,
            ),
        )
        await goal_db.commit()

        await goal_cadence._check_stale_goals()
        # Should NOT trigger: 8 days < global 30
        assert goal_cadence._signal_queue.empty()

    async def test_stuck_goal_high_priority_signal(self, goal_cadence, goal_db):
        """Stale goal with >= threshold executed proposals → stuck/high-priority."""
        old = (datetime.now(UTC) - timedelta(days=20)).isoformat()
        await goal_db.execute(
            "INSERT INTO user_goals "
            "(id, title, category, priority, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("goal-stuck", "Stuck Goal", "project", "high", "active", old, old),
        )
        for i in range(2):  # GOAL_STUCK_EXECUTED_THRESHOLD
            await goal_db.execute(
                "INSERT INTO ego_proposals "
                "(id, action_type, content, status, goal_id, created_at) "
                "VALUES (?, 'investigate', 'x', 'executed', ?, ?)",
                (f"p-stuck-{i}", "goal-stuck", datetime.now(UTC).isoformat()),
            )
        await goal_db.commit()

        await goal_cadence._check_stale_goals()

        signals = goal_cadence._signal_queue.drain()
        assert len(signals) == 1
        sig = signals[0]
        assert sig.priority == "high"
        assert sig.metadata["mode"] == "stuck"
        assert sig.metadata["executed_proposals"] == 2
        assert "stuck" in sig.summary.lower()

    async def test_worked_but_below_threshold_is_stale(self, goal_cadence, goal_db):
        """Stale goal with < threshold executed proposals stays stale/medium.

        Also asserts non-executed (pending) proposals do NOT count as effort.
        """
        old = (datetime.now(UTC) - timedelta(days=20)).isoformat()
        await goal_db.execute(
            "INSERT INTO user_goals "
            "(id, title, category, priority, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("goal-onep", "One Proposal", "project", "medium", "active", old, old),
        )
        await goal_db.execute(
            "INSERT INTO ego_proposals "
            "(id, action_type, content, status, goal_id, created_at) "
            "VALUES (?, 'investigate', 'x', 'executed', ?, ?)",
            ("p-onep-0", "goal-onep", datetime.now(UTC).isoformat()),
        )
        # A pending proposal must NOT count toward "executed" effort.
        await goal_db.execute(
            "INSERT INTO ego_proposals "
            "(id, action_type, content, status, goal_id, created_at) "
            "VALUES (?, 'investigate', 'x', 'pending', ?, ?)",
            ("p-onep-1", "goal-onep", datetime.now(UTC).isoformat()),
        )
        await goal_db.commit()

        await goal_cadence._check_stale_goals()

        signals = goal_cadence._signal_queue.drain()
        assert len(signals) == 1
        sig = signals[0]
        assert sig.priority == "medium"
        assert sig.metadata["mode"] == "stale"
        assert sig.metadata["executed_proposals"] == 1

    async def test_genesis_origin_goal_not_scanned(self, goal_cadence, goal_db):
        """PR-3a: the user-ego scanner reviews USER goals only — a stale
        origin='genesis_ego' goal must not generate a user-facing goal_review
        signal (the genesis ego reviews its own lane separately)."""
        twenty_days_ago = (datetime.now(UTC) - timedelta(days=20)).isoformat()
        await goal_db.execute(
            "INSERT INTO user_goals "
            "(id, title, category, priority, status, origin, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "goal-ego",
                "Ego Ops Goal",
                "project",
                "high",
                "active",
                "genesis_ego",
                twenty_days_ago,
                twenty_days_ago,
            ),
        )
        await goal_db.commit()

        await goal_cadence._check_stale_goals()
        assert goal_cadence._signal_queue.empty()


class TestJobHealthKeyPerEgo:
    """Each ego records job health under its OWN key, never a shared 'ego_cycle'.

    Both EgoCadenceManagers write to the single global job_health table via
    GenesisRuntime.record_job_success/failure. Sharing the literal key
    'ego_cycle' made the two egos clobber each other's health row — one ego's
    cycle success/failure overwrote the other's. The key must be the ego's own
    source_tag ('user_ego_cycle' / 'genesis_ego_cycle'), matching the existing
    ego_source convention used everywhere else.
    """

    @pytest.mark.parametrize("source_tag", ["user_ego_cycle", "genesis_ego_cycle"])
    async def test_record_success_uses_per_ego_key(
        self,
        cadence,
        source_tag,
        monkeypatch,
    ):
        cadence._session._source_tag = source_tag
        mock_rt = MagicMock()
        monkeypatch.setattr(
            "genesis.runtime.GenesisRuntime.instance",
            classmethod(lambda cls: mock_rt),
        )
        cadence._record_success()
        mock_rt.record_job_success.assert_called_once_with(source_tag)

    @pytest.mark.parametrize("source_tag", ["user_ego_cycle", "genesis_ego_cycle"])
    async def test_record_failure_uses_per_ego_key(
        self,
        cadence,
        source_tag,
        monkeypatch,
    ):
        cadence._session._source_tag = source_tag
        mock_rt = MagicMock()
        monkeypatch.setattr(
            "genesis.runtime.GenesisRuntime.instance",
            classmethod(lambda cls: mock_rt),
        )
        cadence._record_failure("boom")
        mock_rt.record_job_failure.assert_called_once_with(source_tag, "boom")

    async def test_two_egos_write_distinct_keys(
        self,
        config,
        mock_idle_detector,
        db,
        monkeypatch,
    ):
        """The core guarantee: user and genesis egos never collide on one key."""
        recorded: list[str] = []
        mock_rt = MagicMock()
        mock_rt.record_job_success.side_effect = recorded.append
        monkeypatch.setattr(
            "genesis.runtime.GenesisRuntime.instance",
            classmethod(lambda cls: mock_rt),
        )
        for tag in ("user_ego_cycle", "genesis_ego_cycle"):
            session = AsyncMock()
            session._source_tag = tag
            mgr = EgoCadenceManager(
                session=session,
                config=config,
                idle_detector=mock_idle_detector,
                db=db,
            )
            mgr._record_success()
        assert recorded == ["user_ego_cycle", "genesis_ego_cycle"]
        assert len(set(recorded)) == 2  # no collision


# ---------------------------------------------------------------------------
# Signal TTLs — every producer stamps a lifetime (signal-resilience PR-1)
# ---------------------------------------------------------------------------


class TestSignalTTLs:
    """Producers stamp expires_at so parked/requeued signals age out
    instead of going stale in the queue. Escalations carry NO TTL —
    critical facts don't expire on their own.
    """

    @staticmethod
    def _ttl_minutes(sig) -> float:
        assert sig.expires_at is not None, "producer must stamp expires_at"
        expires = datetime.fromisoformat(sig.expires_at)
        return (expires - datetime.now(UTC)).total_seconds() / 60

    async def test_proactive_tick_ttl_matches_current_interval(self, cadence):
        await cadence._on_tick()
        [sig] = cadence._signal_queue.drain()
        ttl = self._ttl_minutes(sig)
        assert cadence._current_interval - 1 < ttl <= cadence._current_interval

    async def test_morning_report_ttl_six_hours(self, cadence):
        await cadence._on_morning_report()
        [sig] = cadence._signal_queue.drain()
        assert sig.focus_category == "daily_briefing"
        ttl = self._ttl_minutes(sig)
        assert 6 * 60 - 1 < ttl <= 6 * 60

    async def test_reactive_ttl_twelve_hours(self, cadence):
        cadence._running = True
        cadence.push_reactive_event({"type": "test", "summary": "an error"})
        [sig] = cadence._signal_queue.drain()
        assert sig.focus_category == "reactive"
        ttl = self._ttl_minutes(sig)
        assert 12 * 60 - 1 < ttl <= 12 * 60

    async def test_escalation_has_no_ttl(self, cadence):
        cadence._running = True
        cadence.push_escalation_event({"type": "test", "summary": "critical"})
        [sig] = cadence._signal_queue.drain()
        assert sig.focus_category == "escalation"
        assert sig.expires_at is None


# ---------------------------------------------------------------------------
# Interval persistence across restarts (WS-3)
# ---------------------------------------------------------------------------


class TestIntervalPersistence:
    @pytest.fixture(autouse=True)
    def _isolate_config(self, monkeypatch, config):
        """Prevent hot-reload from overriding the test config with disk values."""
        monkeypatch.setattr(
            "genesis.ego.config.load_ego_config",
            lambda: config,
        )

    async def test_backoff_persisted_then_restored(
        self,
        cadence,
        mock_session,
        config,
        db,
        mock_idle_detector,
    ):
        mock_session._source_tag = "user_ego_cycle"
        mock_session.run_unified_cycle.return_value = EgoCycle(
            output_text="quiet",
            proposals_json="[]",
            focus_summary="idle",
            ego_source="user_ego_cycle",
        )
        await cadence._on_tick()
        await cadence._process_signals()
        backed_off = cadence.current_interval_minutes
        assert backed_off > config.cadence_minutes

        from genesis.db.crud import ego as ego_crud

        stored = await ego_crud.get_state(db, "cadence_interval:user_ego_cycle")
        assert stored == str(backed_off)

        # A fresh manager on the same db restores the backed-off interval,
        # instead of resetting to base as it did before this change.
        fresh = EgoCadenceManager(
            session=mock_session,
            config=config,
            idle_detector=mock_idle_detector,
            db=db,
        )
        assert fresh.current_interval_minutes == config.cadence_minutes
        await fresh._restore_interval()
        assert fresh.current_interval_minutes == backed_off

    async def test_restore_clamps_to_max(self, cadence, mock_session, db):
        mock_session._source_tag = "user_ego_cycle"
        from genesis.db.crud import ego as ego_crud

        await ego_crud.set_state(
            db,
            key="cadence_interval:user_ego_cycle",
            value="99999",
        )
        await cadence._restore_interval()
        assert cadence.current_interval_minutes == cadence._config.max_interval_minutes

    async def test_restore_no_row_keeps_base(self, cadence, mock_session, db):
        mock_session._source_tag = "genesis_ego_cycle"
        await cadence._restore_interval()
        assert cadence.current_interval_minutes == cadence._config.cadence_minutes

    async def test_restore_invalid_value_keeps_base(self, cadence, mock_session, db):
        mock_session._source_tag = "user_ego_cycle"
        from genesis.db.crud import ego as ego_crud

        await ego_crud.set_state(
            db,
            key="cadence_interval:user_ego_cycle",
            value="not-an-int",
        )
        await cadence._restore_interval()
        assert cadence.current_interval_minutes == cadence._config.cadence_minutes

    async def test_restore_keeps_recency_expanded_backoff(
        self,
        cadence,
        mock_session,
        db,
    ):
        """A persisted interval above the static cap survives restart when the
        user has been away long enough for the recency ceiling to expand."""
        mock_session._source_tag = "user_ego_cycle"
        # User last active 5 days ago → recency tier raises the ceiling to 1440.
        await _insert_foreground_session(
            db,
            last_activity_at=(datetime.now(UTC) - timedelta(days=5)).isoformat(),
        )
        from genesis.db.crud import ego as ego_crud

        await ego_crud.set_state(
            db,
            key="cadence_interval:user_ego_cycle",
            value="1440",
        )
        await cadence._restore_interval()
        # NOT clamped down to the static config cap (240).
        assert cadence.current_interval_minutes == 1440

    async def test_last_proactive_fire_persisted_and_restored(
        self,
        cadence,
        mock_session,
        db,
        mock_idle_detector,
        config,
    ):
        """The quiet-hours floor survives a restart: last proactive fire time is
        persisted on tick and restored by a fresh manager."""
        mock_session._source_tag = "user_ego_cycle"
        from genesis.db.crud import ego as ego_crud

        stamp = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
        await ego_crud.set_state(
            db,
            key="last_proactive_fire:user_ego_cycle",
            value=stamp,
        )
        fresh = EgoCadenceManager(
            session=mock_session,
            config=config,
            idle_detector=mock_idle_detector,
            db=db,
        )
        assert fresh._last_proactive_fire_at is None
        await fresh._restore_last_proactive_fire()
        assert fresh._last_proactive_fire_at is not None
        assert fresh._last_proactive_fire_at.isoformat() == stamp

    async def test_boot_first_fire_uses_restored_interval(
        self,
        cadence,
        mock_session,
        db,
    ):
        mock_session._source_tag = "user_ego_cycle"
        from genesis.db.crud import ego as ego_crud

        await ego_crud.set_state(
            db,
            key="cadence_interval:user_ego_cycle",
            value="180",
        )
        await cadence._restore_interval()
        assert cadence.current_interval_minutes == 180
        last = datetime.now(UTC) - timedelta(minutes=10)
        await _seed_last_success(db, "user_ego_cycle", last)
        first_fire = await cadence._compute_boot_first_fire()
        expected = last + timedelta(minutes=180)  # restored interval, not base
        assert abs((first_fire - expected).total_seconds()) < 2


# ---------------------------------------------------------------------------
# Quiet-hours floor (circadian model) — proactive ticks only
# ---------------------------------------------------------------------------


class TestQuietHours:
    def _mgr(self, db, mock_session, mock_idle_detector, **overrides):
        cfg_kwargs = {
            "cadence_minutes": 60,
            "max_interval_minutes": 240,
            "quiet_hours_enabled": True,
            "quiet_hours_start": 23,
            "quiet_hours_end": 7,
            "quiet_hours_min_interval_minutes": 240,
        }
        cfg_kwargs.update(overrides)
        cfg = EgoConfig(**cfg_kwargs)
        mock_session._source_tag = "user_ego_cycle"
        return EgoCadenceManager(
            session=mock_session,
            config=cfg,
            idle_detector=mock_idle_detector,
            db=db,
        )

    @staticmethod
    def _at(hour: int):
        import datetime as _dt

        return _dt.datetime(2026, 1, 1, hour, 0, tzinfo=_dt.UTC)

    def test_suppress_mode_skips_entire_window(
        self, db, mock_session, mock_idle_detector, monkeypatch,
    ):
        mgr = self._mgr(
            db, mock_session, mock_idle_detector, quiet_hours_mode="suppress",
        )
        now = self._at(2)
        monkeypatch.setattr("genesis.ego.cadence._local_now", lambda tz: now)
        monkeypatch.setattr("genesis.ego.cadence._now_utc", lambda: now)
        # No prior fire AND a very recent fire both suppress in suppress mode.
        mgr._last_proactive_fire_at = None
        assert mgr._quiet_hours_suppresses_tick() is True
        mgr._last_proactive_fire_at = now - timedelta(minutes=1)
        assert mgr._quiet_hours_suppresses_tick() is True

    def test_floor_mode_is_default_and_throttles(
        self, db, mock_session, mock_idle_detector, monkeypatch,
    ):
        mgr = self._mgr(db, mock_session, mock_idle_detector)  # default mode
        assert mgr._config.quiet_hours_mode == "floor"
        now = self._at(2)
        monkeypatch.setattr("genesis.ego.cadence._local_now", lambda tz: now)
        monkeypatch.setattr("genesis.ego.cadence._now_utc", lambda: now)
        mgr._last_proactive_fire_at = now - timedelta(minutes=1)  # recent → throttled
        assert mgr._quiet_hours_suppresses_tick() is True
        mgr._last_proactive_fire_at = None  # no prior fire → allowed
        assert mgr._quiet_hours_suppresses_tick() is False

    def test_suppress_mode_reschedules_past_window(
        self, db, mock_session, mock_idle_detector, monkeypatch,
    ):
        from unittest.mock import MagicMock

        mgr = self._mgr(
            db, mock_session, mock_idle_detector, quiet_hours_mode="suppress",
        )
        now = self._at(2)  # inside the 23→7 window
        monkeypatch.setattr("genesis.ego.cadence._local_now", lambda tz: now)
        mgr._scheduler = MagicMock()
        mgr._reschedule_past_quiet_hours()
        assert mgr._scheduler.modify_job.called
        _, kwargs = mgr._scheduler.modify_job.call_args
        # Next fire advanced to the window end (07:00), not left in-window.
        assert kwargs["next_run_time"].hour == 7

    def test_in_quiet_hours_crosses_midnight(self, db, mock_session, mock_idle_detector):
        mgr = self._mgr(db, mock_session, mock_idle_detector)  # 23 → 7
        assert mgr._in_quiet_hours(self._at(23)) is True
        assert mgr._in_quiet_hours(self._at(2)) is True
        assert mgr._in_quiet_hours(self._at(6)) is True
        assert mgr._in_quiet_hours(self._at(7)) is False  # end is exclusive
        assert mgr._in_quiet_hours(self._at(12)) is False
        assert mgr._in_quiet_hours(self._at(22)) is False

    def test_in_quiet_hours_same_day_window(self, db, mock_session, mock_idle_detector):
        mgr = self._mgr(
            db,
            mock_session,
            mock_idle_detector,
            quiet_hours_start=1,
            quiet_hours_end=5,
        )
        assert mgr._in_quiet_hours(self._at(0)) is False
        assert mgr._in_quiet_hours(self._at(1)) is True
        assert mgr._in_quiet_hours(self._at(4)) is True
        assert mgr._in_quiet_hours(self._at(5)) is False

    def test_zero_width_window_never_in(self, db, mock_session, mock_idle_detector):
        mgr = self._mgr(
            db,
            mock_session,
            mock_idle_detector,
            quiet_hours_start=3,
            quiet_hours_end=3,
        )
        assert mgr._in_quiet_hours(self._at(3)) is False

    def test_suppresses_inside_window_when_recent(
        self,
        db,
        mock_session,
        mock_idle_detector,
        monkeypatch,
    ):
        mgr = self._mgr(db, mock_session, mock_idle_detector)
        now = self._at(2)  # 02:00 local, inside window
        monkeypatch.setattr("genesis.ego.cadence._local_now", lambda tz: now)
        monkeypatch.setattr("genesis.ego.cadence._now_utc", lambda: now)
        mgr._last_proactive_fire_at = now - timedelta(minutes=30)  # < 240 floor
        assert mgr._quiet_hours_suppresses_tick() is True

    def test_allows_inside_window_without_prior_fire(
        self,
        db,
        mock_session,
        mock_idle_detector,
        monkeypatch,
    ):
        mgr = self._mgr(db, mock_session, mock_idle_detector)
        now = self._at(2)
        monkeypatch.setattr("genesis.ego.cadence._local_now", lambda tz: now)
        monkeypatch.setattr("genesis.ego.cadence._now_utc", lambda: now)
        mgr._last_proactive_fire_at = None
        assert mgr._quiet_hours_suppresses_tick() is False

    def test_allows_inside_window_when_floor_elapsed(
        self,
        db,
        mock_session,
        mock_idle_detector,
        monkeypatch,
    ):
        mgr = self._mgr(db, mock_session, mock_idle_detector)
        now = self._at(6)
        monkeypatch.setattr("genesis.ego.cadence._local_now", lambda tz: now)
        monkeypatch.setattr("genesis.ego.cadence._now_utc", lambda: now)
        mgr._last_proactive_fire_at = now - timedelta(minutes=300)  # > 240 floor
        assert mgr._quiet_hours_suppresses_tick() is False

    def test_allows_outside_window(
        self,
        db,
        mock_session,
        mock_idle_detector,
        monkeypatch,
    ):
        mgr = self._mgr(db, mock_session, mock_idle_detector)
        now = self._at(14)  # daytime
        monkeypatch.setattr("genesis.ego.cadence._local_now", lambda tz: now)
        monkeypatch.setattr("genesis.ego.cadence._now_utc", lambda: now)
        mgr._last_proactive_fire_at = now - timedelta(minutes=1)  # very recent
        assert mgr._quiet_hours_suppresses_tick() is False

    def test_disabled_never_suppresses(
        self,
        db,
        mock_session,
        mock_idle_detector,
        monkeypatch,
    ):
        mgr = self._mgr(db, mock_session, mock_idle_detector, quiet_hours_enabled=False)
        now = self._at(2)
        monkeypatch.setattr("genesis.ego.cadence._local_now", lambda tz: now)
        monkeypatch.setattr("genesis.ego.cadence._now_utc", lambda: now)
        mgr._last_proactive_fire_at = now - timedelta(minutes=1)
        assert mgr._quiet_hours_suppresses_tick() is False

    async def test_on_tick_suppressed_pushes_nothing(
        self,
        db,
        mock_session,
        mock_idle_detector,
        monkeypatch,
    ):
        mgr = self._mgr(db, mock_session, mock_idle_detector)
        now = self._at(2)
        monkeypatch.setattr("genesis.ego.cadence._local_now", lambda tz: now)
        monkeypatch.setattr("genesis.ego.cadence._now_utc", lambda: now)
        mgr._last_proactive_fire_at = now - timedelta(minutes=30)
        before = mgr._proactive_cycle_count
        await mgr._on_tick()
        assert mgr._signal_queue.empty()
        assert mgr._proactive_cycle_count == before  # no counter slot consumed

    async def test_morning_report_not_gated_by_quiet_hours(
        self,
        db,
        mock_session,
        mock_idle_detector,
        monkeypatch,
    ):
        mgr = self._mgr(db, mock_session, mock_idle_detector)
        now = self._at(2)
        monkeypatch.setattr("genesis.ego.cadence._local_now", lambda tz: now)
        monkeypatch.setattr("genesis.ego.cadence._now_utc", lambda: now)
        mgr._last_proactive_fire_at = now - timedelta(minutes=5)  # would gate a tick
        await mgr._on_morning_report()
        assert not mgr._signal_queue.empty()
        [sig] = mgr._signal_queue.drain()
        assert sig.focus_category == "daily_briefing"


# ---------------------------------------------------------------------------
# Dashboard gated-approval helper (WS-3)
# ---------------------------------------------------------------------------


class TestPendingCliApproval:
    async def test_detects_pending_ego_approval(self, db):
        from genesis.db.crud import ego as ego_crud

        await db.execute(TABLES["approval_requests"])
        await db.execute(
            "INSERT INTO approval_requests "
            "(id, action_type, action_class, description, context, status) "
            "VALUES ('a1', 'autonomous_cli_fallback', 'costly_reversible', "
            "'Approve Claude Code session for user ego cycle?', "
            '\'{"policy_id": "user_ego_cycle", "subsystem": "ego"}\', \'pending\')',
        )
        await db.commit()
        assert await ego_crud.has_pending_cli_approval(db, "user_ego_cycle") is True
        # Both egos share subsystem="ego"; the distinct policy_id is what keeps
        # the user ego's pending approval from cross-stalling the genesis ego.
        assert await ego_crud.has_pending_cli_approval(db, "genesis_ego_cycle") is False

    async def test_ignores_resolved_and_other_types(self, db):
        from genesis.db.crud import ego as ego_crud

        await db.execute(TABLES["approval_requests"])
        await db.execute(
            "INSERT INTO approval_requests "
            "(id, action_type, action_class, description, context, status) "
            "VALUES ('a2', 'autonomous_cli_fallback', 'costly_reversible', "
            "'Approve Claude Code session for user ego cycle?', "
            "'{\"policy_id\": \"user_ego_cycle\"}', 'approved')",
        )
        await db.commit()
        assert await ego_crud.has_pending_cli_approval(db, "user_ego_cycle") is False

    async def test_matches_on_policy_id_not_description(self, db):
        """A reworded approval message must not break the gate: matching keys on
        context.policy_id, never the human-readable description text."""
        from genesis.db.crud import ego as ego_crud

        await db.execute(TABLES["approval_requests"])
        await db.execute(
            "INSERT INTO approval_requests "
            "(id, action_type, action_class, description, context, status) "
            "VALUES ('a3', 'autonomous_cli_fallback', 'costly_reversible', "
            "'Totally reworded approval prompt with no matchable label', "
            "'{\"policy_id\": \"user_ego_cycle\"}', 'pending')",
        )
        await db.commit()
        assert await ego_crud.has_pending_cli_approval(db, "user_ego_cycle") is True


# ---------------------------------------------------------------------------
# Gate-aware consumer — signals survive a gated window (WS-2)
# ---------------------------------------------------------------------------


class TestGateAwareConsumer:
    @pytest.fixture(autouse=True)
    def _isolate_config(self, monkeypatch, config):
        monkeypatch.setattr(
            "genesis.ego.config.load_ego_config",
            lambda: config,
        )

    async def test_paused_preserves_signals_without_draining(
        self,
        cadence,
        mock_session,
    ):
        """Pre-drain gate: a paused ego leaves queued signals intact."""
        cadence._running = True
        cadence.push_reactive_event({"type": "t", "summary": "keep me"})
        assert len(cadence._signal_queue) == 1
        cadence.pause()
        status = await cadence._process_signals()
        assert status == "gated"
        assert len(cadence._signal_queue) == 1  # NOT drained/lost
        mock_session.run_unified_cycle.assert_not_called()

    async def test_approval_pending_preserves_signals(
        self,
        cadence,
        mock_session,
        monkeypatch,
    ):
        """Pre-flight: a pending CLI approval skips drain + context assembly."""
        cadence._running = True
        cadence.push_reactive_event({"type": "t", "summary": "briefing"})
        monkeypatch.setattr(cadence, "_approval_pending", AsyncMock(return_value=True))
        status = await cadence._process_signals()
        assert status == "gated"
        assert len(cadence._signal_queue) == 1
        mock_session.run_unified_cycle.assert_not_called()

    async def test_cycle_blocked_requeues_when_approval_pending(
        self,
        cadence,
        mock_session,
        monkeypatch,
    ):
        """Safety net: a pending-approval block at dispatch time requeues."""
        cadence._running = True
        cadence.push_reactive_event({"type": "t", "summary": "survive the block"})
        mock_session.run_unified_cycle.side_effect = CycleBlockedError("approval pending")
        # Pre-flight false (no row yet), but the dispatch created a pending row —
        # so the post-block re-check sees it. Model that ordering:
        pending = iter([False, True])
        monkeypatch.setattr(
            cadence,
            "_approval_pending",
            AsyncMock(side_effect=lambda: next(pending)),
        )
        status = await cadence._process_signals()
        assert status == "gated"
        assert len(cadence._signal_queue) == 1  # requeued
        [sig] = cadence._signal_queue.drain()
        assert sig.summary == "survive the block"

    async def test_terminal_block_does_not_requeue(
        self,
        cadence,
        mock_session,
        monkeypatch,
    ):
        """A terminal block (no pending approval row ever) drops signals rather
        than looping the consumer forever on a no-TTL escalation."""
        cadence._running = True
        cadence.push_escalation_event({"type": "t", "summary": "cli disabled"})
        mock_session.run_unified_cycle.side_effect = CycleBlockedError("cli fallback disabled")
        # _approval_pending stays False throughout: no resolvable row exists.
        monkeypatch.setattr(cadence, "_approval_pending", AsyncMock(return_value=False))
        status = await cadence._process_signals()
        assert status == "ran"  # attempted + dropped, NOT gated
        assert len(cadence._signal_queue) == 0  # not requeued → no loop

    async def test_empty_and_ran_statuses(self, cadence, mock_session):
        cadence._running = True
        assert await cadence._process_signals() == "empty"
        cadence.push_reactive_event({"type": "t", "summary": "go"})
        assert await cadence._process_signals() == "ran"

    async def test_deep_think_not_burned_by_gated_attempt(
        self,
        cadence,
        mock_session,
        monkeypatch,
    ):
        """A gated attempt consumes no deep-think slot — the Opus upgrade still
        lands on the Nth COMPLETED proactive cycle (WS-2d)."""
        sonnet = EgoConfig(
            cadence_minutes=60,
            model="sonnet",
            quiet_hours_enabled=False,
        )
        cadence._config = sonnet
        monkeypatch.setattr("genesis.ego.config.load_ego_config", lambda: sonnet)
        cadence._deep_think_interval = 2
        mock_session.run_unified_cycle.return_value = EgoCycle(
            output_text="x",
            proposals_json="[]",
            focus_summary="f",
            ego_source="user_ego_cycle",
        )

        # Attempt 1: gated by a pending approval → no completion, no slot spent.
        monkeypatch.setattr(cadence, "_approval_pending", AsyncMock(return_value=True))
        await cadence._on_tick()
        assert await cadence._process_signals() == "gated"
        assert cadence._completed_proactive_count == 0

        # Ungate → completed proactive cycle 1: sonnet (1 % 2 != 0).
        monkeypatch.setattr(cadence, "_approval_pending", AsyncMock(return_value=False))
        assert await cadence._process_signals() == "ran"  # drains the queued tick
        assert cadence._completed_proactive_count == 1
        assert mock_session.run_unified_cycle.call_args.kwargs.get("model_override") is None

        # Completed proactive cycle 2: deep-think → opus (2 % 2 == 0).
        mock_session.run_unified_cycle.reset_mock()
        await cadence._on_tick()
        assert await cadence._process_signals() == "ran"
        assert cadence._completed_proactive_count == 2
        assert mock_session.run_unified_cycle.call_args.kwargs.get("model_override") == "opus"

    async def test_on_tick_no_longer_sets_model_override(self, cadence):
        """Deep-think moved to consume time — _on_tick pushes a plain signal."""
        cadence._deep_think_interval = 1  # would have fired every tick under old logic
        cadence._config = EgoConfig(model="sonnet", quiet_hours_enabled=False)
        await cadence._on_tick()
        [sig] = cadence._signal_queue.drain()
        assert sig.metadata.get("model_override") is None
