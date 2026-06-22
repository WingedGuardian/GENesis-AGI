"""Tests for follow_ups CRUD — retention cleanup + four-axis foundation."""

import pytest

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


# ─── Four-axis foundation: kind / domain / goal_id ───────────────────────────


async def test_create_with_kind_and_domain(db):
    """create() persists kind, domain, and goal_id."""
    fid = await follow_ups.create(
        db, **_BASE, kind="tabled", domain="internal", goal_id="g1",
    )
    row = await follow_ups.get_by_id(db, fid)
    assert row["kind"] == "tabled"
    assert row["domain"] == "internal"
    assert row["goal_id"] == "g1"


async def test_create_defaults(db):
    """Defaults: kind='follow_up', domain NULL, goal_id NULL."""
    fid = await follow_ups.create(db, **_BASE)
    row = await follow_ups.get_by_id(db, fid)
    assert row["kind"] == "follow_up"
    assert row["domain"] is None
    assert row["goal_id"] is None


async def test_get_actionable_excludes_tabled(db):
    """Tabled follow-ups never appear in the actionable feed unless opted in."""
    fu = await follow_ups.create(db, **_BASE)
    tabled = await follow_ups.create(db, **_BASE, kind="tabled")

    ids = {r["id"] for r in await follow_ups.get_actionable(db)}
    assert fu in ids
    assert tabled not in ids

    ids_all = {r["id"] for r in await follow_ups.get_actionable(db, include_tabled=True)}
    assert tabled in ids_all


async def test_get_pending_excludes_tabled(db):
    """The dispatcher/pending feeds also exclude tabled by default."""
    tabled = await follow_ups.create(db, **_BASE, kind="tabled")
    ids = {r["id"] for r in await follow_ups.get_pending(db)}
    assert tabled not in ids


async def test_set_kind_moves_lane(db):
    fid = await follow_ups.create(db, **_BASE)
    assert await follow_ups.set_kind(db, fid, "tabled")
    row = await follow_ups.get_by_id(db, fid)
    assert row["kind"] == "tabled"


async def test_set_kind_rejects_invalid(db):
    fid = await follow_ups.create(db, **_BASE)
    with pytest.raises(ValueError):
        await follow_ups.set_kind(db, fid, "bogus")


async def test_set_domain_and_clear(db):
    fid = await follow_ups.create(db, **_BASE)
    assert await follow_ups.set_domain(db, fid, "user_world")
    assert (await follow_ups.get_by_id(db, fid))["domain"] == "user_world"
    assert await follow_ups.set_domain(db, fid, None)
    assert (await follow_ups.get_by_id(db, fid))["domain"] is None


async def test_delete_removes_row(db):
    fid = await follow_ups.create(db, **_BASE)
    assert await follow_ups.delete(db, fid)
    assert await follow_ups.get_by_id(db, fid) is None


async def test_query_page_filters_search_and_null_domain(db):
    await follow_ups.create(
        db, **{**_BASE, "content": "alpha internal item"}, domain="internal",
    )
    await follow_ups.create(
        db, **{**_BASE, "content": "beta user item"}, domain="user_world",
    )
    await follow_ups.create(db, **{**_BASE, "content": "gamma unclassified"})

    internal = await follow_ups.query_page(db, domain="internal")
    assert internal and all(r["domain"] == "internal" for r in internal)

    hits = await follow_ups.query_page(db, search="alpha")
    assert len(hits) == 1 and "alpha" in hits[0]["content"]

    nulls = await follow_ups.query_page(db, domain="__null__")
    assert nulls and all(r["domain"] is None for r in nulls)

    total = await follow_ups.count_filtered(db)
    assert total == 3


async def test_query_page_rejects_nothing_on_bad_sort(db):
    """Unknown sort falls back to priority instead of raising/injecting."""
    await follow_ups.create(db, **_BASE)
    rows = await follow_ups.query_page(db, sort="; DROP TABLE follow_ups; --")
    assert len(rows) == 1


async def test_set_priority_updates(db):
    fid = await follow_ups.create(db, **_BASE)
    assert await follow_ups.set_priority(db, fid, "critical")
    assert (await follow_ups.get_by_id(db, fid))["priority"] == "critical"


async def test_set_priority_rejects_invalid(db):
    fid = await follow_ups.create(db, **_BASE)
    with pytest.raises(ValueError):
        await follow_ups.set_priority(db, fid, "urgent")


async def test_set_priority_unknown_id_returns_false(db):
    assert await follow_ups.set_priority(db, "nope", "high") is False


async def test_delete_batch_removes_all(db):
    ids = [await follow_ups.create(db, **_BASE) for _ in range(3)]
    assert await follow_ups.delete_batch(db, ids) == 3
    for fid in ids:
        assert await follow_ups.get_by_id(db, fid) is None
    # Empty selection is a no-op, not an error.
    assert await follow_ups.delete_batch(db, []) == 0


async def test_set_kind_batch_moves_lane(db):
    ids = [await follow_ups.create(db, **_BASE) for _ in range(2)]
    assert await follow_ups.set_kind_batch(db, ids, "tabled") == 2
    for fid in ids:
        assert (await follow_ups.get_by_id(db, fid))["kind"] == "tabled"


async def test_set_kind_batch_rejects_invalid(db):
    fid = await follow_ups.create(db, **_BASE)
    with pytest.raises(ValueError):
        await follow_ups.set_kind_batch(db, [fid], "bogus")


async def test_update_status_batch_stamps_completed_at(db):
    ids = [await follow_ups.create(db, **_BASE) for _ in range(2)]
    assert await follow_ups.update_status_batch(
        db, ids, "completed", resolution_notes="bulk done"
    ) == 2
    for fid in ids:
        row = await follow_ups.get_by_id(db, fid)
        assert row["status"] == "completed"
        assert row["completed_at"] is not None
        assert row["resolution_notes"] == "bulk done"


async def test_update_status_batch_rejects_invalid_status(db):
    fid = await follow_ups.create(db, **_BASE)
    with pytest.raises(ValueError):
        await follow_ups.update_status_batch(db, [fid], "cancelled")
