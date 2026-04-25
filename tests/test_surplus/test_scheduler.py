"""Tests for SurplusScheduler — dispatch loop + brainstorm orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from genesis.surplus.compute_availability import ComputeAvailability
from genesis.surplus.executor import StubExecutor
from genesis.surplus.idle_detector import IdleDetector
from genesis.surplus.queue import SurplusQueue
from genesis.surplus.scheduler import SurplusScheduler
from genesis.surplus.types import ComputeTier, TaskType

pytestmark = pytest.mark.asyncio


def _make_scheduler(db, *, idle=True, lmstudio_up=False, enable_code_audits=False):
    idle_detector = IdleDetector()
    if idle:
        idle_detector._last_activity_at = datetime.now(UTC) - timedelta(minutes=30)
    else:
        idle_detector.mark_active()
    compute = ComputeAvailability(lmstudio_url="http://fake:1234/v1/models")
    return SurplusScheduler(
        db=db, queue=SurplusQueue(db), idle_detector=idle_detector,
        compute_availability=compute, executor=StubExecutor(),
        enable_code_audits=enable_code_audits,
    ), compute


async def test_dispatch_skips_when_not_idle(db):
    sched, compute = _make_scheduler(db, idle=False)
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        result = await sched.dispatch_once()
    assert result is False


async def test_dispatch_skips_when_queue_empty(db):
    sched, compute = _make_scheduler(db, idle=True)
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        result = await sched.dispatch_once()
    assert result is False


async def test_dispatch_processes_task(db):
    sched, compute = _make_scheduler(db, idle=True)
    queue = sched._queue
    await queue.enqueue(TaskType.BRAINSTORM_USER, ComputeTier.FREE_API, 0.8, "cooperation")
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        result = await sched.dispatch_once()
    assert result is True
    assert await queue.pending_count() == 0


async def test_dispatch_writes_staging_entry(db):
    sched, compute = _make_scheduler(db, idle=True)
    queue = sched._queue
    await queue.enqueue(TaskType.BRAINSTORM_USER, ComputeTier.FREE_API, 0.8, "cooperation")
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        await sched.dispatch_once()
    cursor = await db.execute("SELECT COUNT(*) FROM surplus_insights")
    row = await cursor.fetchone()
    assert row[0] == 1


async def test_dispatch_skips_local_30b_when_lmstudio_down(db):
    sched, compute = _make_scheduler(db, idle=True)
    queue = sched._queue
    await queue.enqueue(TaskType.BRAINSTORM_SELF, ComputeTier.LOCAL_30B, 0.8, "curiosity")
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        result = await sched.dispatch_once()
    assert result is False
    assert await queue.pending_count() == 1


async def test_dispatch_processes_local_30b_when_lmstudio_up(db):
    sched, compute = _make_scheduler(db, idle=True)
    queue = sched._queue
    await queue.enqueue(TaskType.BRAINSTORM_SELF, ComputeTier.LOCAL_30B, 0.8, "curiosity")
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=True):
        result = await sched.dispatch_once()
    assert result is True
    assert await queue.pending_count() == 0


async def test_dispatch_handles_executor_error(db):
    sched, compute = _make_scheduler(db, idle=True)
    queue = sched._queue
    await queue.enqueue(TaskType.BRAINSTORM_USER, ComputeTier.FREE_API, 0.8, "cooperation")

    async def _boom(task):
        raise RuntimeError("kaboom")

    sched._executor.execute = _boom
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        result = await sched.dispatch_once()
    assert result is False
    # Task should be marked failed, not pending
    assert await queue.pending_count() == 0


async def test_brainstorm_check_schedules_sessions(db):
    sched, compute = _make_scheduler(db, idle=True)
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        await sched.brainstorm_check()
    assert await sched._queue.pending_count() == 2


async def test_start_and_stop(db):
    sched, compute = _make_scheduler(db, idle=True, enable_code_audits=True)
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        await sched.start()
    assert sched._scheduler.running is True
    # Verify jobs were registered
    assert sched._scheduler.get_job("surplus_brainstorm_check") is not None
    assert sched._scheduler.get_job("surplus_dispatch") is not None
    assert sched._scheduler.get_job("schedule_code_audit") is not None
    # Brainstorm (2) + code audit (1) + code index (1)
    # + maintenance (4) + analytical (1: gap_clustering)
    # + pipeline (1: prompt_effectiveness step 1) = 10
    assert await sched._queue.pending_count() == 10
    # Stop should not raise
    await sched.stop()


async def test_start_without_code_audits(db):
    sched, compute = _make_scheduler(db, idle=True, enable_code_audits=False)
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        await sched.start()
    assert sched._scheduler.running is True
    # Code audit job should NOT be registered
    assert sched._scheduler.get_job("schedule_code_audit") is None
    # Brainstorm (2) + code index (1)
    # + maintenance (4) + analytical (1: gap_clustering)
    # + pipeline (1: prompt_effectiveness step 1) = 9
    assert await sched._queue.pending_count() == 9
    await sched.stop()


async def test_schedule_code_audit_noop_when_disabled(db):
    sched, compute = _make_scheduler(db, idle=True, enable_code_audits=False)
    # Direct call should be a no-op
    await sched.schedule_code_audit()
    assert await sched._queue.pending_count() == 0


async def test_dispatch_drains_expired_tasks(db):
    sched, compute = _make_scheduler(db, idle=True)
    # Insert an old task directly
    old_time = (datetime.now(UTC) - timedelta(hours=100)).isoformat()
    await db.execute(
        "INSERT INTO surplus_tasks (id, task_type, compute_tier, priority, drive_alignment, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("old-task-1", "brainstorm_self", "free_api", 0.5, "curiosity", "pending", old_time),
    )
    await db.commit()
    with patch.object(compute, "_ping_lmstudio", new_callable=AsyncMock, return_value=False):
        await sched.dispatch_once()
    # Expired task should be drained
    cursor = await db.execute("SELECT COUNT(*) FROM surplus_tasks WHERE id = 'old-task-1' AND status = 'pending'")
    row = await cursor.fetchone()
    assert row[0] == 0
