"""Tests for genesis.reflection.scheduler — weekly reflection scheduling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables, seed_data
from genesis.perception.types import ReflectionResult
from genesis.reflection.scheduler import ReflectionScheduler


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


@pytest.fixture
def mock_bridge():
    bridge = AsyncMock()
    bridge.run_weekly_assessment = AsyncMock(
        return_value=ReflectionResult(success=True, reason="done"),
    )
    bridge.run_quality_calibration = AsyncMock(
        return_value=ReflectionResult(success=True, reason="done"),
    )
    return bridge


@pytest.fixture
def mock_stability():
    stability = AsyncMock()
    stability.check_regression = AsyncMock(return_value=False)
    stability.emit_regression_signal = AsyncMock()
    return stability


@pytest.fixture
def scheduler(db, mock_bridge, mock_stability):
    return ReflectionScheduler(
        bridge=mock_bridge,
        stability_monitor=mock_stability,
        db=db,
    )


class TestSchedulerLifecycle:
    @pytest.mark.asyncio
    async def test_start_and_stop(self, scheduler):
        await scheduler.start()
        assert scheduler.is_running
        await scheduler.stop()
        assert not scheduler.is_running

    @pytest.mark.asyncio
    async def test_not_running_before_start(self, scheduler):
        assert not scheduler.is_running

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self, scheduler):
        await scheduler.stop()  # Should not raise


class TestAssessmentJob:
    @pytest.mark.asyncio
    async def test_runs_assessment(self, scheduler, mock_bridge, db):
        await scheduler._run_assessment()
        mock_bridge.run_weekly_assessment.assert_called_once_with(db)

    @pytest.mark.asyncio
    async def test_skips_if_already_ran(self, scheduler, mock_bridge, db):
        # Insert a recent self_assessment observation
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES ('a1', 'test', 'self_assessment', '{}', 'medium', ?)",
            (now,),
        )
        await db.commit()

        await scheduler._run_assessment()
        mock_bridge.run_weekly_assessment.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_if_old_assessment(self, scheduler, mock_bridge, db):
        # Insert an assessment from last week
        old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES ('a1', 'test', 'self_assessment', '{}', 'medium', ?)",
            (old,),
        )
        await db.commit()

        await scheduler._run_assessment()
        mock_bridge.run_weekly_assessment.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_assessment_failure(self, scheduler, mock_bridge, db):
        mock_bridge.run_weekly_assessment.return_value = ReflectionResult(
            success=False, reason="failed",
        )
        await scheduler._run_assessment()  # Should not raise

    @pytest.mark.asyncio
    async def test_handles_assessment_exception(self, scheduler, mock_bridge, db):
        mock_bridge.run_weekly_assessment.side_effect = RuntimeError("boom")
        await scheduler._run_assessment()  # Should not raise


class TestCalibrationJob:
    @pytest.mark.asyncio
    async def test_runs_calibration(self, scheduler, mock_bridge, db):
        await scheduler._run_calibration()
        mock_bridge.run_quality_calibration.assert_called_once_with(db)

    @pytest.mark.asyncio
    async def test_regression_check_after_calibration(
        self, scheduler, mock_stability, db,
    ):
        await scheduler._run_calibration()
        mock_stability.check_regression.assert_called_once_with(db)

    @pytest.mark.asyncio
    async def test_regression_detected_emits_signal(
        self, scheduler, mock_stability, db,
    ):
        mock_stability.check_regression.return_value = True
        await scheduler._run_calibration()
        mock_stability.emit_regression_signal.assert_called_once_with(db)

    @pytest.mark.asyncio
    async def test_no_regression_no_signal(self, scheduler, mock_stability, db):
        mock_stability.check_regression.return_value = False
        await scheduler._run_calibration()
        mock_stability.emit_regression_signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_if_already_ran(self, scheduler, mock_bridge, db):
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES ('c1', 'test', 'quality_calibration', '{}', 'medium', ?)",
            (now,),
        )
        await db.commit()

        await scheduler._run_calibration()
        mock_bridge.run_quality_calibration.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_stability_monitor(self, db, mock_bridge):
        sched = ReflectionScheduler(
            bridge=mock_bridge,
            stability_monitor=None,
            db=db,
        )
        await sched._run_calibration()
        mock_bridge.run_quality_calibration.assert_called_once()


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_no_observations_means_not_ran(self, scheduler, db):
        result = await scheduler._already_ran_this_week("self_assessment")
        assert not result

    @pytest.mark.asyncio
    async def test_recent_observation_means_ran(self, scheduler, db):
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES ('a1', 'test', 'self_assessment', '{}', 'medium', ?)",
            (now,),
        )
        await db.commit()
        result = await scheduler._already_ran_this_week("self_assessment")
        assert result


class TestRuntimeBootstrap:
    @pytest.mark.asyncio
    async def test_init_reflection_creates_components(self, db):
        """Verify runtime._init_reflection() creates Phase 7 components."""
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime()
        GenesisRuntime.reset()

        # Manually set prerequisites
        rt._db = db
        rt._event_bus = MagicMock()

        # Create a mock bridge
        mock_bridge = AsyncMock()
        mock_bridge.set_context_gatherer = MagicMock()
        mock_bridge.set_output_router = MagicMock()
        rt._cc_reflection_bridge = mock_bridge

        await rt._init_reflection()

        assert rt._stability_monitor is not None
        assert rt._reflection_scheduler is not None
        mock_bridge.set_context_gatherer.assert_called_once()
        mock_bridge.set_output_router.assert_called_once()

        # Cleanup
        await rt._reflection_scheduler.stop()

    @pytest.mark.asyncio
    async def test_init_reflection_skips_without_bridge(self, db):
        """Without bridge, reflection init is skipped gracefully."""
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime()
        GenesisRuntime.reset()
        rt._db = db
        rt._cc_reflection_bridge = None

        await rt._init_reflection()
        assert rt._reflection_scheduler is None
        assert rt._stability_monitor is None
