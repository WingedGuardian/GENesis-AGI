"""Tests for follow_ups CRUD — retention cleanup."""

from genesis.db.crud import follow_ups

_BASE = dict(
    source="test",
    content="test follow-up",
    reason="testing",
    strategy="ego_judgment",
    priority="medium",
)


async def test_purge_completed_deletes_old(db):
    """Old completed follow-ups are deleted."""
    fid = await follow_ups.create(db, **_BASE)
    # Mark completed with an old timestamp
    await db.execute(
        "UPDATE follow_ups SET status = 'completed', "
        "completed_at = '2025-01-01T00:00:00+00:00' WHERE id = ?",
        (fid,),
    )
    await db.commit()

    count = await follow_ups.purge_completed(db)
    assert count == 1
    row = await follow_ups.get_by_id(db, fid)
    assert row is None


async def test_purge_completed_keeps_recent(db):
    """Recently completed follow-ups survive."""
    fid = await follow_ups.create(db, **_BASE)
    await follow_ups.update_status(db, fid, status="completed",
                                   resolution_notes="done")

    count = await follow_ups.purge_completed(db)
    assert count == 0
    row = await follow_ups.get_by_id(db, fid)
    assert row is not None


async def test_purge_completed_keeps_pinned(db):
    """Pinned follow-ups are never purged regardless of age."""
    fid = await follow_ups.create(db, **_BASE)
    await db.execute(
        "UPDATE follow_ups SET status = 'completed', pinned = 1, "
        "completed_at = '2025-01-01T00:00:00+00:00' WHERE id = ?",
        (fid,),
    )
    await db.commit()

    count = await follow_ups.purge_completed(db)
    assert count == 0
    row = await follow_ups.get_by_id(db, fid)
    assert row is not None


async def test_purge_completed_keeps_pending(db):
    """Pending follow-ups are never touched."""
    fid = await follow_ups.create(db, **_BASE)

    count = await follow_ups.purge_completed(db)
    assert count == 0
    row = await follow_ups.get_by_id(db, fid)
    assert row is not None
    assert row["status"] == "pending"


async def test_purge_failed_old(db):
    """Old failed follow-ups are also purged."""
    fid = await follow_ups.create(db, **_BASE)
    await db.execute(
        "UPDATE follow_ups SET status = 'failed', "
        "completed_at = '2025-01-01T00:00:00+00:00' WHERE id = ?",
        (fid,),
    )
    await db.commit()

    count = await follow_ups.purge_completed(db)
    assert count == 1
