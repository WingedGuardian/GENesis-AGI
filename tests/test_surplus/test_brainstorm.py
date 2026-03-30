"""Tests for BrainstormRunner — daily brainstorm scheduling and execution."""

from __future__ import annotations

from datetime import datetime

import pytest

from genesis.db.crud import brainstorm as brainstorm_crud
from genesis.db.crud import surplus as surplus_crud
from genesis.surplus.brainstorm import BrainstormRunner
from genesis.surplus.queue import SurplusQueue
from genesis.surplus.types import ComputeTier, TaskType

pytestmark = pytest.mark.asyncio


def _fixed_clock(dt_str: str = "2026-03-04T10:00:00+00:00"):
    dt = datetime.fromisoformat(dt_str)
    return lambda: dt


@pytest.fixture
def queue(db):
    return SurplusQueue(db, clock=_fixed_clock())


@pytest.fixture
def runner(db, queue):
    return BrainstormRunner(db, queue, clock=_fixed_clock())


async def test_schedules_two_sessions(runner, queue):
    """schedule_daily_brainstorms enqueues exactly 2 tasks."""
    await runner.schedule_daily_brainstorms()
    assert await queue.pending_count() == 2


async def test_idempotent_scheduling(db, queue):
    """Second schedule call after execution is a no-op."""
    clock = _fixed_clock()
    runner = BrainstormRunner(db, queue, clock=clock)

    # First schedule + execute both brainstorms
    await runner.schedule_daily_brainstorms()
    await runner.execute_brainstorm(TaskType.BRAINSTORM_USER, "cooperation")
    await runner.execute_brainstorm(TaskType.BRAINSTORM_SELF, "competence")

    logs_before = await brainstorm_crud.list_by_type(db, "upgrade_user", limit=10)
    count_before = len(logs_before)

    # Second schedule should be a no-op (logs exist for today)
    await runner.schedule_daily_brainstorms()

    logs_after = await brainstorm_crud.list_by_type(db, "upgrade_user", limit=10)
    assert len(logs_after) == count_before


async def test_schedules_both_types(db, queue):
    """Both brainstorm_user and brainstorm_self task types are queued."""
    runner = BrainstormRunner(db, queue, clock=_fixed_clock())
    await runner.schedule_daily_brainstorms()

    # Drain both tasks and check types
    types_found = set()
    for _ in range(2):
        task = await queue.next_task([ComputeTier.FREE_API])
        if task:
            types_found.add(task.task_type)
            await queue.mark_running(task.id)
            await queue.mark_completed(task.id)

    assert TaskType.BRAINSTORM_USER in types_found
    assert TaskType.BRAINSTORM_SELF in types_found


async def test_execute_brainstorm_stub_returns_none(db, queue):
    """StubExecutor results are not persisted — they're noise (confidence=0.0)."""
    runner = BrainstormRunner(db, queue, clock=_fixed_clock())
    staging_id = await runner.execute_brainstorm(TaskType.BRAINSTORM_USER, "cooperation")
    assert staging_id is None  # Stub results are skipped


async def test_execute_brainstorm_real_executor_writes_staging(db, queue):
    """Real executor results are written to surplus_insights with promotion_status='pending'."""
    from genesis.surplus.types import ExecutorResult

    class FakeExecutor:
        async def execute(self, task):
            return ExecutorResult(
                success=True,
                content="Real insight",
                insights=[{"generating_model": "test-model", "confidence": 0.8}],
            )

    runner = BrainstormRunner(db, queue, clock=_fixed_clock(), executor=FakeExecutor())
    staging_id = await runner.execute_brainstorm(TaskType.BRAINSTORM_USER, "cooperation")

    assert staging_id is not None
    row = await surplus_crud.get_by_id(db, staging_id)
    assert row is not None
    assert row["promotion_status"] == "pending"
    assert row["source_task_type"] == "brainstorm_user"
    assert row["drive_alignment"] == "cooperation"


async def test_execute_brainstorm_stub_writes_no_log(db, queue):
    """StubExecutor results are skipped — no brainstorm log written."""
    runner = BrainstormRunner(db, queue, clock=_fixed_clock())
    await runner.execute_brainstorm(TaskType.BRAINSTORM_USER, "cooperation")

    logs = await brainstorm_crud.list_by_type(db, "upgrade_user", limit=5)
    assert len(logs) == 0  # Stubs are no longer persisted


async def test_new_day_allows_new_sessions(db, queue):
    """Day 2 clock allows new brainstorm sessions after day 1 logs exist."""
    day1_clock = _fixed_clock("2026-03-04T10:00:00+00:00")
    runner1 = BrainstormRunner(db, queue, clock=day1_clock)
    await runner1.schedule_daily_brainstorms()
    await runner1.execute_brainstorm(TaskType.BRAINSTORM_USER, "cooperation")
    await runner1.execute_brainstorm(TaskType.BRAINSTORM_SELF, "competence")

    # Drain day 1 pending tasks
    while True:
        t = await queue.next_task([ComputeTier.FREE_API])
        if not t:
            break
        await queue.mark_running(t.id)
        await queue.mark_completed(t.id)

    # Day 2
    day2_clock = _fixed_clock("2026-03-05T10:00:00+00:00")
    queue2 = SurplusQueue(db, clock=day2_clock)
    runner2 = BrainstormRunner(db, queue2, clock=day2_clock)
    await runner2.schedule_daily_brainstorms()

    assert await queue2.pending_count() == 2
