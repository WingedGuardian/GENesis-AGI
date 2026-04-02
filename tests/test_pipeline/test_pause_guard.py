"""Tests: pipeline cycle and brainstorm_check respect Genesis pause state."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest


@pytest.mark.asyncio
async def test_pipeline_cycle_skips_when_paused():
    """run_pipeline_cycle should return without calling orchestrator when paused."""
    from genesis.runtime.init.pipeline import run_pipeline_cycle

    rt = MagicMock()
    type(rt).paused = PropertyMock(return_value=True)
    rt._pipeline_orchestrator = AsyncMock()

    await run_pipeline_cycle(rt, "test-profile")

    rt._pipeline_orchestrator.run_cycle.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_cycle_runs_when_not_paused():
    """run_pipeline_cycle should call orchestrator when not paused."""
    from genesis.runtime.init.pipeline import run_pipeline_cycle

    rt = MagicMock()
    type(rt).paused = PropertyMock(return_value=False)
    rt._pipeline_orchestrator = AsyncMock(
        run_cycle=AsyncMock(
            return_value=MagicMock(tier0_collected=5, tier1_survived=3, discarded=2)
        )
    )
    rt.record_job_success = MagicMock()

    await run_pipeline_cycle(rt, "test-profile")

    rt._pipeline_orchestrator.run_cycle.assert_called_once_with("test-profile")


@pytest.mark.asyncio
async def test_pipeline_cycle_fails_closed_on_pause_error():
    """If pause check raises, cycle should NOT proceed (fail closed)."""
    from genesis.runtime.init.pipeline import run_pipeline_cycle

    rt = MagicMock()
    type(rt).paused = PropertyMock(side_effect=RuntimeError("broken"))
    rt._pipeline_orchestrator = AsyncMock()

    await run_pipeline_cycle(rt, "test-profile")

    rt._pipeline_orchestrator.run_cycle.assert_not_called()


@pytest.mark.asyncio
async def test_brainstorm_check_skips_when_paused(db):
    """brainstorm_check should return without scheduling when paused."""
    from genesis.surplus.queue import SurplusQueue
    from genesis.surplus.scheduler import SurplusScheduler

    queue = SurplusQueue(db)
    # Minimal scheduler — deps don't matter since we'll skip
    sched = SurplusScheduler.__new__(SurplusScheduler)
    sched._db = db
    sched._queue = queue
    sched._brainstorm_runner = AsyncMock()
    sched._event_bus = None

    mock_rt = MagicMock()
    mock_rt.paused = True
    mock_rt.record_job_success = MagicMock()

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        await sched.brainstorm_check()

    sched._brainstorm_runner.schedule_daily_brainstorms.assert_not_called()
