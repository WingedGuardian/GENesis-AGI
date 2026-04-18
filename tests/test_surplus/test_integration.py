"""Integration tests for surplus infrastructure — full pipeline tests."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from genesis.surplus.compute_availability import ComputeAvailability
from genesis.surplus.executor import StubExecutor
from genesis.surplus.idle_detector import IdleDetector
from genesis.surplus.queue import SurplusQueue
from genesis.surplus.scheduler import SurplusScheduler
from genesis.surplus.types import ComputeTier, TaskType


def _fixed_clock(dt_str="2026-03-04T10:00:00+00:00"):
    dt = datetime.fromisoformat(dt_str)
    return lambda: dt


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_enqueue_idle_dispatch_staging(self, db):
        """Full pipeline: enqueue → idle → dispatch → staging entry created."""
        clock = _fixed_clock()
        idle = IdleDetector(clock=clock)
        idle._last_activity_at = datetime.fromisoformat("2026-03-04T09:30:00+00:00")
        compute = ComputeAvailability(lmstudio_url="http://fake:1234/v1/models")
        queue = SurplusQueue(db, clock=clock)
        from genesis.surplus.types import ExecutorResult

        class FakeExecutor:
            async def execute(self, task):
                return ExecutorResult(
                    success=True, content="Test surplus insight with sufficient content length to pass the quality gate",
                    insights=[{"generating_model": "test-model", "confidence": 0.8}],
                )

        scheduler = SurplusScheduler(
            db=db, queue=queue, idle_detector=idle,
            compute_availability=compute, executor=FakeExecutor(), clock=clock,
        )

        await queue.enqueue(TaskType.BRAINSTORM_USER, ComputeTier.FREE_API, 0.8, "curiosity")

        with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
            dispatched = await scheduler.dispatch_once()
        assert dispatched is True

        from genesis.db.crud import surplus as surplus_crud
        pending = await surplus_crud.list_pending(db)
        assert len(pending) >= 1
        assert pending[0]["source_task_type"] == "brainstorm_user"

    @pytest.mark.asyncio
    async def test_brainstorm_full_lifecycle(self, db):
        """Brainstorm runner → queue → executor → surplus_insights + brainstorm_log."""
        clock = _fixed_clock()
        idle = IdleDetector(clock=clock)
        idle._last_activity_at = datetime.fromisoformat("2026-03-04T09:30:00+00:00")
        compute = ComputeAvailability(lmstudio_url="http://fake:1234/v1/models")
        from genesis.surplus.types import ExecutorResult

        class FakeExecutor:
            async def execute(self, task):
                return ExecutorResult(
                    success=True, content="Test surplus insight with sufficient content length to pass the quality gate",
                    insights=[{"generating_model": "test-model", "confidence": 0.8}],
                )

        queue = SurplusQueue(db, clock=clock)
        scheduler = SurplusScheduler(
            db=db, queue=queue, idle_detector=idle,
            compute_availability=compute, executor=FakeExecutor(), clock=clock,
        )

        with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
            await scheduler.brainstorm_check()
            assert await queue.pending_count() == 2

            # Dispatch both tasks
            await scheduler.dispatch_once()
            await scheduler.dispatch_once()
            assert await queue.pending_count() == 0

        from genesis.db.crud import surplus as surplus_crud
        staging = await surplus_crud.list_pending(db)
        assert len(staging) >= 2

    @pytest.mark.asyncio
    async def test_compute_availability_gates_local_tasks(self, db):
        """LOCAL_30B tasks wait when LM Studio is down."""
        clock = _fixed_clock()
        idle = IdleDetector(clock=clock)
        idle._last_activity_at = datetime.fromisoformat("2026-03-04T09:30:00+00:00")
        compute = ComputeAvailability(lmstudio_url="http://fake:1234/v1/models")
        queue = SurplusQueue(db, clock=clock)
        scheduler = SurplusScheduler(
            db=db, queue=queue, idle_detector=idle,
            compute_availability=compute, executor=StubExecutor(), clock=clock,
        )

        await queue.enqueue(TaskType.BRAINSTORM_USER, ComputeTier.LOCAL_30B, 0.9, "curiosity")

        # LM Studio down — task stays pending
        with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
            dispatched = await scheduler.dispatch_once()
        assert dispatched is False
        assert await queue.pending_count() == 1

        # LM Studio up — invalidate cache so new ping result is used
        compute._lmstudio_cached = None
        compute._lmstudio_cached_at = None
        with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=True):
            dispatched = await scheduler.dispatch_once()
        assert dispatched is True
        assert await queue.pending_count() == 0

    @pytest.mark.asyncio
    async def test_priority_ordering_across_tasks(self, db):
        """Higher priority tasks dispatch first."""
        clock = _fixed_clock()
        idle = IdleDetector(clock=clock)
        idle._last_activity_at = datetime.fromisoformat("2026-03-04T09:30:00+00:00")
        compute = ComputeAvailability(lmstudio_url="http://fake:1234/v1/models")
        queue = SurplusQueue(db, clock=clock)
        scheduler = SurplusScheduler(
            db=db, queue=queue, idle_detector=idle,
            compute_availability=compute, executor=StubExecutor(), clock=clock,
        )

        await queue.enqueue(TaskType.BRAINSTORM_SELF, ComputeTier.FREE_API, 0.3, "competence")
        await queue.enqueue(TaskType.BRAINSTORM_USER, ComputeTier.FREE_API, 0.9, "curiosity")

        with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
            dispatched = await scheduler.dispatch_once()
        assert dispatched is True
        # The lower-priority one should remain
        assert await queue.pending_count() == 1

    @pytest.mark.asyncio
    async def test_failed_task_respects_max_attempts(self, db):
        """Failed tasks increment attempt_count."""
        clock = _fixed_clock()
        idle = IdleDetector(clock=clock)
        idle._last_activity_at = datetime.fromisoformat("2026-03-04T09:30:00+00:00")
        compute = ComputeAvailability(lmstudio_url="http://fake:1234/v1/models")
        queue = SurplusQueue(db, clock=clock)

        class FailingExecutor:
            async def execute(self, task):
                raise RuntimeError("always fails")

        scheduler = SurplusScheduler(
            db=db, queue=queue, idle_detector=idle,
            compute_availability=compute, executor=FailingExecutor(), clock=clock,
        )

        await queue.enqueue(TaskType.BRAINSTORM_USER, ComputeTier.FREE_API, 0.8, "curiosity")

        with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
            dispatched = await scheduler.dispatch_once()
        assert dispatched is False

        # Check the task was marked failed with attempt_count incremented
        cursor = await db.execute(
            "SELECT * FROM surplus_tasks WHERE status = 'failed'"
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        assert len(rows) == 1
        assert rows[0]["attempt_count"] == 1
