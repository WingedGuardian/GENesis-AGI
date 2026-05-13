"""Tests for surplus_tasks CRUD operations."""

import pytest

from genesis.db.crud import surplus_tasks


@pytest.mark.asyncio
async def test_create_and_get(db):
    rid = await surplus_tasks.create(
        db,
        id="st-1",
        task_type="brainstorm",
        compute_tier="slm",
        priority=0.7,
        drive_alignment="curiosity",
        created_at="2026-03-04T00:00:00Z",
        payload='{"topic": "test"}',
    )
    assert rid == "st-1"
    row = await surplus_tasks.get_by_id(db, "st-1")
    assert row is not None
    assert row["task_type"] == "brainstorm"
    assert row["compute_tier"] == "slm"
    assert row["priority"] == 0.7
    assert row["drive_alignment"] == "curiosity"
    assert row["status"] == "pending"
    assert row["payload"] == '{"topic": "test"}'
    assert row["attempt_count"] == 0


@pytest.mark.asyncio
async def test_next_task_priority_order(db):
    for i, pri in enumerate([0.3, 0.9, 0.6]):
        await surplus_tasks.create(
            db,
            id=f"st-{i}",
            task_type="brainstorm",
            compute_tier="slm",
            priority=pri,
            drive_alignment="curiosity",
            created_at=f"2026-03-04T0{i}:00:00Z",
        )
    task = await surplus_tasks.next_task(db, available_tiers=["slm"])
    assert task["id"] == "st-1"  # priority 0.9


@pytest.mark.asyncio
async def test_next_task_filters_by_tier(db):
    await surplus_tasks.create(
        db, id="st-a", task_type="brainstorm", compute_tier="slm",
        priority=0.9, drive_alignment="curiosity", created_at="2026-03-04T00:00:00Z",
    )
    await surplus_tasks.create(
        db, id="st-b", task_type="brainstorm", compute_tier="sonnet",
        priority=0.5, drive_alignment="curiosity", created_at="2026-03-04T00:00:00Z",
    )
    task = await surplus_tasks.next_task(db, available_tiers=["sonnet"])
    assert task["id"] == "st-b"


@pytest.mark.asyncio
async def test_next_task_skips_non_pending(db):
    await surplus_tasks.create(
        db, id="st-r", task_type="brainstorm", compute_tier="slm",
        priority=0.9, drive_alignment="curiosity", created_at="2026-03-04T00:00:00Z",
    )
    await surplus_tasks.mark_running(db, "st-r", started_at="2026-03-04T00:01:00Z")
    task = await surplus_tasks.next_task(db, available_tiers=["slm"])
    assert task is None


@pytest.mark.asyncio
async def test_mark_running(db):
    await surplus_tasks.create(
        db, id="st-1", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-03-04T00:00:00Z",
    )
    ok = await surplus_tasks.mark_running(db, "st-1", started_at="2026-03-04T00:01:00Z")
    assert ok is True
    row = await surplus_tasks.get_by_id(db, "st-1")
    assert row["status"] == "running"
    assert row["started_at"] == "2026-03-04T00:01:00Z"


@pytest.mark.asyncio
async def test_mark_completed(db):
    await surplus_tasks.create(
        db, id="st-1", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-03-04T00:00:00Z",
    )
    await surplus_tasks.mark_running(db, "st-1", started_at="2026-03-04T00:01:00Z")
    ok = await surplus_tasks.mark_completed(
        db, "st-1", completed_at="2026-03-04T00:02:00Z", result_staging_id="rs-1",
    )
    assert ok is True
    row = await surplus_tasks.get_by_id(db, "st-1")
    assert row["status"] == "completed"
    assert row["completed_at"] == "2026-03-04T00:02:00Z"
    assert row["result_staging_id"] == "rs-1"


@pytest.mark.asyncio
async def test_mark_failed(db):
    await surplus_tasks.create(
        db, id="st-1", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-03-04T00:00:00Z",
    )
    ok = await surplus_tasks.mark_failed(db, "st-1", failure_reason="timeout")
    assert ok is True
    row = await surplus_tasks.get_by_id(db, "st-1")
    assert row["status"] == "failed"
    assert row["failure_reason"] == "timeout"
    assert row["attempt_count"] == 1
    # Fail again to verify increment
    await surplus_tasks.mark_failed(db, "st-1", failure_reason="timeout again")
    row = await surplus_tasks.get_by_id(db, "st-1")
    assert row["attempt_count"] == 2


@pytest.mark.asyncio
async def test_drain_expired(db):
    await surplus_tasks.create(
        db, id="st-old", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-03-01T00:00:00Z",
    )
    await surplus_tasks.create(
        db, id="st-new", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-03-04T00:00:00Z",
    )
    # Mark st-old as running — drain should NOT touch it (only pending)
    await surplus_tasks.create(
        db, id="st-old-running", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-03-01T00:00:00Z",
    )
    await surplus_tasks.mark_running(db, "st-old-running", started_at="2026-03-04T00:00:00Z")
    count = await surplus_tasks.drain_expired(db, before="2026-03-03T00:00:00Z")
    assert count == 1
    assert await surplus_tasks.get_by_id(db, "st-old") is None
    assert await surplus_tasks.get_by_id(db, "st-new") is not None
    assert await surplus_tasks.get_by_id(db, "st-old-running") is not None


@pytest.mark.asyncio
async def test_count_pending(db):
    assert await surplus_tasks.count_pending(db) == 0
    await surplus_tasks.create(
        db, id="st-1", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-03-04T00:00:00Z",
    )
    await surplus_tasks.create(
        db, id="st-2", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-03-04T00:00:00Z",
    )
    assert await surplus_tasks.count_pending(db) == 2
    await surplus_tasks.mark_running(db, "st-1", started_at="2026-03-04T00:01:00Z")
    assert await surplus_tasks.count_pending(db) == 1


@pytest.mark.asyncio
async def test_delete(db):
    await surplus_tasks.create(
        db, id="st-1", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-03-04T00:00:00Z",
    )
    assert await surplus_tasks.delete(db, "st-1") is True
    assert await surplus_tasks.get_by_id(db, "st-1") is None
    assert await surplus_tasks.delete(db, "st-1") is False


@pytest.mark.asyncio
async def test_consecutive_failures(db):
    """Counts consecutive failed tasks, stopping at first non-failed."""
    # Create tasks: completed, then 3 failed (newest first by created_at)
    await surplus_tasks.create(
        db, id="st-ok", task_type="backup_verification", compute_tier="slm",
        priority=0.5, drive_alignment="preservation", created_at="2026-03-04T00:00:00Z",
    )
    await surplus_tasks.mark_running(db, "st-ok", started_at="2026-03-04T00:01:00Z")
    await surplus_tasks.mark_completed(db, "st-ok", completed_at="2026-03-04T00:02:00Z")

    for i in range(1, 4):
        tid = f"st-fail-{i}"
        await surplus_tasks.create(
            db, id=tid, task_type="backup_verification", compute_tier="slm",
            priority=0.5, drive_alignment="preservation",
            created_at=f"2026-03-04T0{i}:00:00Z",
        )
        await surplus_tasks.mark_running(db, tid, started_at=f"2026-03-04T0{i}:01:00Z")
        await surplus_tasks.mark_failed(db, tid, failure_reason="test failure")

    count = await surplus_tasks.consecutive_failures(db, "backup_verification")
    assert count == 3

    # Different task type should return 0
    count = await surplus_tasks.consecutive_failures(db, "brainstorm")
    assert count == 0
