"""prune_terminal — retention for finished deferred_work_queue rows (all types)."""

from __future__ import annotations

import pytest

from genesis.db.crud import deferred_work as dw


async def _seed(db, *, id, status, completed_at, work_type="entity_adjudication"):
    await dw.create(
        db,
        id=id,
        work_type=work_type,
        priority=60,
        payload_json="{}",
        deferred_at="2026-01-01T00:00:00+00:00",
        deferred_reason="test",
        created_at="2026-01-01T00:00:00+00:00",
    )
    if status != "pending":
        await dw.update_status(db, id, status=status, completed_at=completed_at)


@pytest.mark.asyncio
async def test_prune_deletes_old_terminal_rows_all_types(db):
    await _seed(
        db,
        id="old-done",
        status="completed",
        completed_at="2026-01-10T00:00:00+00:00",
        work_type="reflection",
    )
    await _seed(
        db,
        id="old-discarded",
        status="discarded",
        completed_at="2026-01-10T00:00:00+00:00",
        work_type="entity_adjudication",
    )
    deleted = await dw.prune_terminal(db, cutoff_iso="2026-06-01T00:00:00+00:00")
    assert deleted == 2
    remaining = await db.execute("SELECT COUNT(*) FROM deferred_work_queue")
    assert (await remaining.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_prune_spares_recent_terminal_rows(db):
    await _seed(db, id="recent-done", status="completed", completed_at="2026-07-16T00:00:00+00:00")
    deleted = await dw.prune_terminal(db, cutoff_iso="2026-06-01T00:00:00+00:00")
    assert deleted == 0


@pytest.mark.asyncio
async def test_prune_spares_pending_and_processing(db):
    await _seed(db, id="pending", status="pending", completed_at=None)
    await _seed(db, id="processing", status="processing", completed_at=None)
    # Both are non-terminal / NULL completed_at — never pruned even if ancient.
    deleted = await dw.prune_terminal(db, cutoff_iso="2027-01-01T00:00:00+00:00")
    assert deleted == 0
    cnt = await db.execute("SELECT COUNT(*) FROM deferred_work_queue")
    assert (await cnt.fetchone())[0] == 2


@pytest.mark.asyncio
async def test_prune_uses_completed_at_not_created_at(db):
    # Row born long ago but completed recently must survive an age test.
    await _seed(
        db, id="late-finisher", status="completed", completed_at="2026-07-16T00:00:00+00:00"
    )
    # created_at is 2026-01-01 (well before cutoff), completed_at is after.
    deleted = await dw.prune_terminal(db, cutoff_iso="2026-06-01T00:00:00+00:00")
    assert deleted == 0
