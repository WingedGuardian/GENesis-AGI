"""Tests for SurplusQueue — drive-weight priority queue over surplus_tasks CRUD."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.db.crud import surplus_tasks
from genesis.surplus.queue import SurplusQueue
from genesis.surplus.types import ComputeTier, TaskType

pytestmark = pytest.mark.asyncio


def _fixed_clock(dt: datetime | None = None):
    t = dt or datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)
    return lambda: t


@pytest.fixture
def queue(db):
    return SurplusQueue(db, clock=_fixed_clock())


async def test_enqueue_returns_id(queue):
    tid = await queue.enqueue(TaskType.BRAINSTORM_SELF, ComputeTier.FREE_API, 0.5, "curiosity")
    assert isinstance(tid, str) and len(tid) > 0


async def test_enqueue_rejects_never_tier(queue):
    with pytest.raises(ValueError, match="NEVER"):
        await queue.enqueue(TaskType.BRAINSTORM_SELF, ComputeTier.NEVER, 0.5, "curiosity")


async def test_next_task_returns_highest_priority(queue):
    await queue.enqueue(TaskType.BRAINSTORM_SELF, ComputeTier.FREE_API, 0.5, "curiosity")
    await queue.enqueue(TaskType.BRAINSTORM_USER, ComputeTier.FREE_API, 0.9, "curiosity")
    task = await queue.next_task([ComputeTier.FREE_API])
    assert task is not None
    assert task.task_type == TaskType.BRAINSTORM_USER


async def test_next_task_filters_by_tier(queue):
    await queue.enqueue(TaskType.BRAINSTORM_SELF, ComputeTier.LOCAL_30B, 0.9, "curiosity")
    task = await queue.next_task([ComputeTier.FREE_API])
    assert task is None


async def test_next_task_returns_none_when_empty(queue):
    task = await queue.next_task([ComputeTier.FREE_API])
    assert task is None


async def test_mark_running_and_completed(db, queue):
    tid = await queue.enqueue(TaskType.BRAINSTORM_SELF, ComputeTier.FREE_API, 0.5, "curiosity")
    await queue.mark_running(tid)
    # Running task should not appear in next_task
    task = await queue.next_task([ComputeTier.FREE_API])
    assert task is None
    await queue.mark_completed(tid, staging_id="stg-123")
    row = await surplus_tasks.get_by_id(db, tid)
    assert row["status"] == "completed"
    assert row["result_staging_id"] == "stg-123"


async def test_mark_failed_increments_attempts(db, queue):
    tid = await queue.enqueue(TaskType.BRAINSTORM_SELF, ComputeTier.FREE_API, 0.5, "curiosity")
    await queue.mark_failed(tid, reason="boom")
    row = await surplus_tasks.get_by_id(db, tid)
    assert row["attempt_count"] == 1
    await queue.mark_failed(tid, reason="boom again")
    row = await surplus_tasks.get_by_id(db, tid)
    assert row["attempt_count"] == 2


async def test_drain_expired_removes_old_tasks(db, queue):
    # Insert an old task directly
    old_time = (datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC) - timedelta(hours=100)).isoformat()
    await surplus_tasks.create(
        db, id="old-task", task_type="brainstorm_self", compute_tier="free_api",
        priority=0.5, drive_alignment="curiosity", created_at=old_time,
    )
    removed = await queue.drain_expired(max_age_hours=72)
    assert removed == 1


async def test_pending_count(queue):
    await queue.enqueue(TaskType.BRAINSTORM_SELF, ComputeTier.FREE_API, 0.5, "curiosity")
    await queue.enqueue(TaskType.BRAINSTORM_USER, ComputeTier.FREE_API, 0.3, "curiosity")
    count = await queue.pending_count()
    assert count == 2


async def test_priority_boosted_by_drive_weight(queue):
    # Both drives have weight 0.25. curiosity: 0.8*0.25=0.2, cooperation: 0.6*0.25=0.15
    await queue.enqueue(TaskType.BRAINSTORM_SELF, ComputeTier.FREE_API, 0.6, "cooperation")
    await queue.enqueue(TaskType.BRAINSTORM_USER, ComputeTier.FREE_API, 0.8, "curiosity")
    task = await queue.next_task([ComputeTier.FREE_API])
    assert task is not None
    assert task.drive_alignment == "curiosity"
    assert abs(task.priority - 0.2) < 1e-9
