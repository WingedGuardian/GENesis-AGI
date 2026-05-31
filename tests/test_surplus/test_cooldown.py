"""Tests for restart-resilient scheduling — cooldown via completed_at."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.db.crud import surplus_tasks
from genesis.surplus.compute_availability import ComputeAvailability
from genesis.surplus.executor import StubExecutor
from genesis.surplus.idle_detector import IdleDetector
from genesis.surplus.queue import SurplusQueue
from genesis.surplus.scheduler import SurplusScheduler
from genesis.surplus.types import ComputeTier, TaskType

pytestmark = pytest.mark.asyncio


def _make_scheduler(db, *, maintenance_hours=24, analytical_hours=12,
                    code_index_hours=4, model_eval_hours=24,
                    j9_eval_batch_hours=24):
    idle_detector = IdleDetector()
    idle_detector._last_activity_at = datetime.now(UTC) - timedelta(minutes=30)
    compute = ComputeAvailability(lmstudio_url="http://fake:1234/v1/models")
    return SurplusScheduler(
        db=db, queue=SurplusQueue(db), idle_detector=idle_detector,
        compute_availability=compute, executor=StubExecutor(),
        enable_code_audits=False,
        maintenance_hours=maintenance_hours,
        analytical_hours=analytical_hours,
        code_index_hours=code_index_hours,
        model_eval_hours=model_eval_hours,
        j9_eval_batch_hours=j9_eval_batch_hours,
    )


# ── CRUD: last_completed_at ──────────────────────────────────────────


async def test_last_completed_at_returns_none_when_empty(db):
    result = await surplus_tasks.last_completed_at(db, "disk_cleanup")
    assert result is None


async def test_last_completed_at_returns_most_recent(db):
    queue = SurplusQueue(db=db)

    # Create and complete two tasks
    t1 = await queue.enqueue(TaskType.DISK_CLEANUP, ComputeTier.FREE_API, 0.4, "preservation")
    await queue.mark_running(t1)
    await surplus_tasks.mark_completed(db, t1, completed_at="2026-05-30T10:00:00+00:00")

    t2 = await queue.enqueue(TaskType.DISK_CLEANUP, ComputeTier.FREE_API, 0.4, "preservation")
    await queue.mark_running(t2)
    await surplus_tasks.mark_completed(db, t2, completed_at="2026-05-30T12:00:00+00:00")

    result = await surplus_tasks.last_completed_at(db, "disk_cleanup")
    assert result == "2026-05-30T12:00:00+00:00"


async def test_last_completed_at_ignores_failed(db):
    queue = SurplusQueue(db=db)

    t1 = await queue.enqueue(TaskType.DISK_CLEANUP, ComputeTier.FREE_API, 0.4, "preservation")
    await queue.mark_running(t1)
    await surplus_tasks.mark_failed(db, t1, failure_reason="test")

    result = await surplus_tasks.last_completed_at(db, "disk_cleanup")
    assert result is None


async def test_last_completed_at_ignores_other_types(db):
    queue = SurplusQueue(db=db)

    t1 = await queue.enqueue(TaskType.BACKUP_VERIFICATION, ComputeTier.FREE_API, 0.7, "preservation")
    await queue.mark_running(t1)
    await surplus_tasks.mark_completed(db, t1, completed_at="2026-05-30T12:00:00+00:00")

    result = await surplus_tasks.last_completed_at(db, "disk_cleanup")
    assert result is None


# ── SurplusScheduler._recently_completed ─────────────────────────────


async def test_recently_completed_true_when_within_window(db):
    sched = _make_scheduler(db, maintenance_hours=24)
    queue = sched._queue

    t1 = await queue.enqueue(TaskType.DISK_CLEANUP, ComputeTier.FREE_API, 0.4, "preservation")
    await queue.mark_running(t1)
    now = datetime.now(UTC)
    await surplus_tasks.mark_completed(
        db, t1, completed_at=(now - timedelta(hours=1)).isoformat(),
    )

    assert await sched._recently_completed(TaskType.DISK_CLEANUP, 24) is True


async def test_recently_completed_false_when_outside_window(db):
    sched = _make_scheduler(db, maintenance_hours=24)
    queue = sched._queue

    t1 = await queue.enqueue(TaskType.DISK_CLEANUP, ComputeTier.FREE_API, 0.4, "preservation")
    await queue.mark_running(t1)
    now = datetime.now(UTC)
    await surplus_tasks.mark_completed(
        db, t1, completed_at=(now - timedelta(hours=25)).isoformat(),
    )

    assert await sched._recently_completed(TaskType.DISK_CLEANUP, 24) is False


async def test_recently_completed_false_when_no_completions(db):
    sched = _make_scheduler(db)
    assert await sched._recently_completed(TaskType.DISK_CLEANUP, 24) is False


# ── schedule_maintenance with cooldown ───────────────────────────────


async def test_schedule_maintenance_skips_recently_completed(db):
    sched = _make_scheduler(db, maintenance_hours=24)
    queue = sched._queue

    # Complete a disk_cleanup task 1 hour ago
    t1 = await queue.enqueue(TaskType.DISK_CLEANUP, ComputeTier.FREE_API, 0.4, "preservation")
    await queue.mark_running(t1)
    now = datetime.now(UTC)
    await surplus_tasks.mark_completed(
        db, t1, completed_at=(now - timedelta(hours=1)).isoformat(),
    )

    # Run schedule_maintenance — should NOT re-enqueue disk_cleanup
    await sched.schedule_maintenance()

    # disk_cleanup should NOT have a new pending task
    assert await queue.active_by_type(TaskType.DISK_CLEANUP) == 0

    # But backup_verification (never completed) should be enqueued
    assert await queue.active_by_type(TaskType.BACKUP_VERIFICATION) == 1


async def test_schedule_maintenance_enqueues_when_cooldown_expired(db):
    sched = _make_scheduler(db, maintenance_hours=24)
    queue = sched._queue

    # Complete a disk_cleanup task 25 hours ago (beyond 24h cooldown)
    t1 = await queue.enqueue(TaskType.DISK_CLEANUP, ComputeTier.FREE_API, 0.4, "preservation")
    await queue.mark_running(t1)
    now = datetime.now(UTC)
    await surplus_tasks.mark_completed(
        db, t1, completed_at=(now - timedelta(hours=25)).isoformat(),
    )

    await sched.schedule_maintenance()

    # disk_cleanup should be re-enqueued
    assert await queue.active_by_type(TaskType.DISK_CLEANUP) == 1


# ── schedule_pipeline with cooldown ──────────────────────────────────


async def test_schedule_pipeline_skips_when_final_step_recently_completed(db):
    sched = _make_scheduler(db, analytical_hours=12)
    queue = sched._queue

    # Complete a PROMPT_EFFECTIVENESS_REVIEW (last step of prompt_effectiveness)
    t1 = await queue.enqueue(
        TaskType.PROMPT_EFFECTIVENESS_REVIEW, ComputeTier.FREE_API, 0.5, "competence",
    )
    await queue.mark_running(t1)
    now = datetime.now(UTC)
    await surplus_tasks.mark_completed(
        db, t1, completed_at=(now - timedelta(hours=1)).isoformat(),
    )

    # schedule_pipeline should skip — final step completed recently
    result = await sched.schedule_pipeline("prompt_effectiveness")
    assert result is None

    # Step 1 should NOT be enqueued
    assert await queue.active_by_type(TaskType.PROMPT_REVIEW_CATALOG) == 0


async def test_schedule_pipeline_enqueues_when_cooldown_expired(db):
    sched = _make_scheduler(db, analytical_hours=12)
    queue = sched._queue

    # Complete final step 13 hours ago (beyond 12h cooldown)
    t1 = await queue.enqueue(
        TaskType.PROMPT_EFFECTIVENESS_REVIEW, ComputeTier.FREE_API, 0.5, "competence",
    )
    await queue.mark_running(t1)
    now = datetime.now(UTC)
    await surplus_tasks.mark_completed(
        db, t1, completed_at=(now - timedelta(hours=13)).isoformat(),
    )

    result = await sched.schedule_pipeline("prompt_effectiveness")
    assert result is not None  # Returns task ID

    # Step 1 should be enqueued
    assert await queue.active_by_type(TaskType.PROMPT_REVIEW_CATALOG) == 1
