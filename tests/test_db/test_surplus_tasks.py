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
    # Backward-compatible default: no verdict passed → NULL.
    assert row["outcome_quality"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["useful", "hollow"])
async def test_mark_completed_records_outcome_quality(db, verdict):
    await surplus_tasks.create(
        db, id="st-q", task_type="brainstorm_self", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-03-04T00:00:00Z",
    )
    ok = await surplus_tasks.mark_completed(
        db, "st-q", completed_at="2026-03-04T00:02:00Z",
        result_staging_id="rs-q", outcome_quality=verdict,
    )
    assert ok is True
    row = await surplus_tasks.get_by_id(db, "st-q")
    assert row["status"] == "completed"
    assert row["outcome_quality"] == verdict


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


# ---------------------------------------------------------------------------
# delete_terminal_before — terminal-row age-cap reaper (WS-F / F7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reap_terminal_deletes_old_completed_keeps_recent(db):
    """Old completed rows are reaped; completed rows newer than the cutoff stay."""
    await surplus_tasks.create(
        db, id="old", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-01-01T00:00:00+00:00",
    )
    await surplus_tasks.mark_completed(db, "old", completed_at="2026-01-01T01:00:00+00:00")
    await surplus_tasks.create(
        db, id="recent", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-06-01T00:00:00+00:00",
    )
    await surplus_tasks.mark_completed(db, "recent", completed_at="2026-06-01T01:00:00+00:00")

    reaped = await surplus_tasks.delete_terminal_before(db, before="2026-03-01T00:00:00+00:00")

    assert reaped == 1
    assert await surplus_tasks.get_by_id(db, "old") is None
    assert await surplus_tasks.get_by_id(db, "recent") is not None


@pytest.mark.asyncio
async def test_reap_terminal_keeps_pending_and_running(db):
    """Non-terminal rows are never reaped, regardless of age."""
    await surplus_tasks.create(
        db, id="old-pending", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-01-01T00:00:00+00:00",
    )
    await surplus_tasks.create(
        db, id="old-running", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-01-01T00:00:00+00:00",
    )
    await surplus_tasks.mark_running(db, "old-running", started_at="2026-01-01T00:30:00+00:00")

    reaped = await surplus_tasks.delete_terminal_before(db, before="2026-03-01T00:00:00+00:00")

    assert reaped == 0
    assert await surplus_tasks.get_by_id(db, "old-pending") is not None
    assert await surplus_tasks.get_by_id(db, "old-running") is not None


@pytest.mark.asyncio
async def test_reap_terminal_ages_failed_and_cancelled_by_fallback(db):
    """Failed rows (no completed_at) age by started_at/created_at; cancelled reaped too."""
    await surplus_tasks.create(
        db, id="old-failed", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-01-01T00:00:00+00:00",
    )
    await surplus_tasks.mark_running(db, "old-failed", started_at="2026-01-01T00:30:00+00:00")
    await surplus_tasks.mark_failed(db, "old-failed", failure_reason="boom")
    await surplus_tasks.create(
        db, id="old-cancelled", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-01-01T00:00:00+00:00",
    )
    await db.execute(
        "UPDATE surplus_tasks SET status = 'cancelled' WHERE id = ?", ("old-cancelled",),
    )
    await db.commit()

    reaped = await surplus_tasks.delete_terminal_before(db, before="2026-03-01T00:00:00+00:00")

    assert reaped == 2
    assert await surplus_tasks.get_by_id(db, "old-failed") is None
    assert await surplus_tasks.get_by_id(db, "old-cancelled") is None


@pytest.mark.asyncio
async def test_reap_terminal_skips_rows_linked_to_open_followup(db):
    """A terminal row referenced by a NON-terminal follow-up is kept; a terminal
    follow-up does not protect it."""
    from genesis.db.crud import follow_ups

    # completed task referenced by an OPEN (scheduled) follow-up -> keep
    await surplus_tasks.create(
        db, id="linked-open", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-01-01T00:00:00+00:00",
    )
    await surplus_tasks.mark_completed(db, "linked-open", completed_at="2026-01-01T01:00:00+00:00")
    fu_open = await follow_ups.create(db, content="c", source="test", strategy="surplus_task")
    await follow_ups.link_task(db, fu_open, "linked-open")  # -> status 'scheduled'

    # completed task referenced only by a COMPLETED follow-up -> reap
    await surplus_tasks.create(
        db, id="linked-closed", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-01-01T00:00:00+00:00",
    )
    await surplus_tasks.mark_completed(db, "linked-closed", completed_at="2026-01-01T01:00:00+00:00")
    fu_closed = await follow_ups.create(db, content="c", source="test", strategy="surplus_task")
    await follow_ups.link_task(db, fu_closed, "linked-closed")
    await follow_ups.update_status(db, fu_closed, "completed")

    reaped = await surplus_tasks.delete_terminal_before(db, before="2026-03-01T00:00:00+00:00")

    assert reaped == 1
    assert await surplus_tasks.get_by_id(db, "linked-open") is not None
    assert await surplus_tasks.get_by_id(db, "linked-closed") is None


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


@pytest.mark.asyncio
async def test_recover_stuck_bumps_attempts_by_default(db):
    """Per-cycle recovery counts each reclaim toward max_retries."""
    await surplus_tasks.create(
        db, id="st-stuck", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-03-04T00:00:00Z",
    )
    await surplus_tasks.mark_running(db, "st-stuck", started_at="2026-03-04T00:01:00Z")

    requeued, failed = await surplus_tasks.recover_stuck_with_retries(db)
    assert (requeued, failed) == (1, 0)
    row = await surplus_tasks.get_by_id(db, "st-stuck")
    assert row["status"] == "pending"
    assert row["attempt_count"] == 1


@pytest.mark.asyncio
async def test_recover_stuck_no_bump_reclaims_all_running(db):
    """Boot sweep: a restart is not a task failure — reclaim every 'running'
    row immediately (older_than_hours=0) without burning attempt_count, and
    never permanently fail a row that only 'ran' across a restart."""
    from datetime import UTC, datetime, timedelta

    recent = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await surplus_tasks.create(
        db, id="st-fresh", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-03-04T00:00:00Z",
    )
    await surplus_tasks.mark_running(db, "st-fresh", started_at=recent)

    # A row already at max_retries: boot sweep must requeue, not fail it.
    await surplus_tasks.create(
        db, id="st-maxed", task_type="brainstorm", compute_tier="slm",
        priority=0.5, drive_alignment="curiosity", created_at="2026-03-04T00:00:00Z",
    )
    await surplus_tasks.mark_running(db, "st-maxed", started_at=recent)
    await db.execute(
        "UPDATE surplus_tasks SET attempt_count = 3 WHERE id = 'st-maxed'"
    )
    await db.commit()

    requeued, failed = await surplus_tasks.recover_stuck_with_retries(
        db, older_than_hours=0, bump_attempt=False,
    )
    assert (requeued, failed) == (2, 0)
    fresh = await surplus_tasks.get_by_id(db, "st-fresh")
    maxed = await surplus_tasks.get_by_id(db, "st-maxed")
    assert fresh["status"] == "pending" and fresh["attempt_count"] == 0
    assert maxed["status"] == "pending" and maxed["attempt_count"] == 3
