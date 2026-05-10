"""Tests for the ego cadence manager."""

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
    """Mock EgoSession with controllable run_cycle."""
    session = AsyncMock()
    session.run_cycle.return_value = EgoCycle(
        id="c1",
        output_text="test",
        proposals_json=json.dumps([{"action_type": "test"}]),
        focus_summary="testing",
        model_used="opus",
        cost_usd=0.15,
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
        await cadence.stop()

    async def test_stop_shuts_down(self, cadence):
        await cadence.start()
        assert cadence.is_running
        await cadence.stop()
        assert not cadence.is_running

    async def test_pause_resume(self, cadence):
        assert not cadence.is_paused
        cadence.pause()
        assert cadence.is_paused
        cadence.resume()
        assert not cadence.is_paused


# ---------------------------------------------------------------------------
# Tick behavior
# ---------------------------------------------------------------------------


class TestCadenceTick:
    async def test_tick_runs_cycle_when_idle(
        self, cadence, mock_session, mock_idle_detector,
    ):
        mock_idle_detector.is_idle.return_value = True
        await cadence._on_tick()
        mock_session.run_cycle.assert_called_once_with()

    async def test_tick_skips_when_active(
        self, cadence, mock_session, mock_idle_detector,
    ):
        mock_idle_detector.is_idle.return_value = False
        await cadence._on_tick()
        mock_session.run_cycle.assert_not_called()

    async def test_tick_skips_when_onboarding_incomplete(
        self, cadence, mock_session, tmp_path,
    ):
        # Remove the marker created by autouse fixture
        (tmp_path / ".genesis" / "setup-complete").unlink()
        await cadence._on_tick()
        mock_session.run_cycle.assert_not_called()

    async def test_tick_skips_when_paused(
        self, cadence, mock_session,
    ):
        cadence.pause()
        await cadence._on_tick()
        mock_session.run_cycle.assert_not_called()

    async def test_tick_skips_when_circuit_open(
        self, cadence, mock_session,
    ):
        cadence._circuit_open_until = datetime.now(UTC) + timedelta(hours=1)
        await cadence._on_tick()
        mock_session.run_cycle.assert_not_called()

    async def test_tick_handles_exception(
        self, cadence, mock_session,
    ):
        mock_session.run_cycle.side_effect = RuntimeError("boom")
        await cadence._on_tick()
        assert cadence.consecutive_failures == 1

    async def test_tick_cycle_blocked_does_not_trip_breaker(
        self, cadence, mock_session,
    ):
        """CycleBlockedError is a gate, not a failure — no circuit breaker impact."""
        mock_session.run_cycle.side_effect = CycleBlockedError("approval pending")
        await cadence._on_tick()
        assert cadence.consecutive_failures == 0

    async def test_morning_report_cycle_blocked_does_not_trip_breaker(
        self, cadence, mock_session,
    ):
        """CycleBlockedError in morning report also doesn't trip breaker."""
        mock_session.run_cycle.side_effect = CycleBlockedError("approval pending")
        await cadence._on_morning_report()
        assert cadence.consecutive_failures == 0


# ---------------------------------------------------------------------------
# Morning report
# ---------------------------------------------------------------------------


class TestMorningReport:
    async def test_morning_report_ignores_idle(
        self, cadence, mock_session, mock_idle_detector,
    ):
        """Morning report runs even when user is active."""
        mock_idle_detector.is_idle.return_value = False
        await cadence._on_morning_report()
        mock_session.run_cycle.assert_called_once_with(is_morning_report=True)

    async def test_morning_report_still_checks_pause(
        self, cadence, mock_session,
    ):
        cadence.pause()
        await cadence._on_morning_report()
        mock_session.run_cycle.assert_not_called()


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    async def test_opens_after_n_failures(self, cadence, mock_session, config):
        mock_session.run_cycle.return_value = None  # failure signal
        for _ in range(config.consecutive_failure_limit):
            await cadence._on_tick()

        assert cadence.consecutive_failures == config.consecutive_failure_limit
        assert cadence._circuit_open_until is not None

        # Next tick should be skipped due to circuit breaker
        mock_session.run_cycle.reset_mock()
        await cadence._on_tick()
        mock_session.run_cycle.assert_not_called()

    async def test_resets_on_success(self, cadence, mock_session):
        # Simulate 2 failures
        mock_session.run_cycle.return_value = None
        await cadence._on_tick()
        await cadence._on_tick()
        assert cadence.consecutive_failures == 2

        # Success resets
        mock_session.run_cycle.return_value = EgoCycle(
            output_text="ok", proposals_json="[]", focus_summary="ok",
        )
        await cadence._on_tick()
        assert cadence.consecutive_failures == 0

    async def test_expires_after_backoff(self, cadence, config):
        cadence._consecutive_failures = config.consecutive_failure_limit
        cadence._circuit_open_until = datetime.now(UTC) - timedelta(seconds=1)

        # Circuit should be closed now (expired)
        assert not cadence._is_circuit_open()
        assert cadence._consecutive_failures == 0


# ---------------------------------------------------------------------------
# Adaptive interval
# ---------------------------------------------------------------------------


class TestAdaptiveInterval:
    async def test_backoff_increases_interval(self, cadence, mock_session, config):
        # Cycle with no proposals → backoff
        mock_session.run_cycle.return_value = EgoCycle(
            output_text="quiet", proposals_json="[]", focus_summary="idle",
        )
        await cadence._on_tick()
        assert cadence.current_interval_minutes == config.cadence_minutes * int(config.backoff_multiplier)

    async def test_proposals_reset_interval(self, cadence, mock_session, config):
        # First: backoff
        mock_session.run_cycle.return_value = EgoCycle(
            output_text="quiet", proposals_json="[]", focus_summary="idle",
        )
        await cadence._on_tick()
        assert cadence.current_interval_minutes > config.cadence_minutes

        # Then: proposals reset
        mock_session.run_cycle.return_value = EgoCycle(
            output_text="active",
            proposals_json=json.dumps([{"action_type": "test"}]),
            focus_summary="active",
        )
        await cadence._on_tick()
        assert cadence.current_interval_minutes == config.cadence_minutes

    async def test_backoff_capped_at_max(self, cadence, mock_session, config):
        """Multiple idle cycles don't exceed max_interval_minutes."""
        mock_session.run_cycle.return_value = EgoCycle(
            output_text="quiet", proposals_json="[]", focus_summary="idle",
        )
        # Run enough times to exceed max
        for _ in range(10):
            await cadence._on_tick()
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

    async def test_backoff_respects_recency_tier(self, cadence, mock_session, db):
        """When user was active 2 days ago, backoff caps at 480 (not 240)."""
        ts = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        await _insert_foreground_session(db, last_activity_at=ts)

        mock_session.run_cycle.return_value = EgoCycle(
            output_text="quiet", proposals_json="[]", focus_summary="idle",
        )
        # Run enough idle cycles to hit the cap
        for _ in range(10):
            await cadence._on_tick()

        assert cadence.current_interval_minutes <= 480
        # Should have backed off beyond the old 240 cap
        assert cadence.current_interval_minutes > 240

    async def test_user_return_resets_to_base(self, cadence, mock_session, db):
        """After a productive cycle, interval resets to base regardless of tier."""
        # Start in a high tier
        old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        await _insert_foreground_session(db, last_activity_at=old_ts)

        mock_session.run_cycle.return_value = EgoCycle(
            output_text="quiet", proposals_json="[]", focus_summary="idle",
        )
        for _ in range(5):
            await cadence._on_tick()
        assert cadence.current_interval_minutes > cadence._config.cadence_minutes

        # Productive cycle → reset to base
        mock_session.run_cycle.return_value = EgoCycle(
            output_text="active",
            proposals_json=json.dumps([{"action_type": "test"}]),
            focus_summary="active",
        )
        await cadence._on_tick()
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
