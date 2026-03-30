"""Tests for surplus_insights CRUD."""

import sqlite3

import pytest

from genesis.db.crud import surplus

_COMMON = dict(
    content="interesting insight",
    source_task_type="research",
    generating_model="opus",
    drive_alignment="curiosity",
    confidence=0.8,
    created_at="2026-01-01T00:00:00",
    ttl="2026-02-01T00:00:00",
)


async def test_create_and_get(db):
    rid = await surplus.create(db, id="s1", **_COMMON)
    assert rid == "s1"
    row = await surplus.get_by_id(db, "s1")
    assert row is not None
    assert row["content"] == "interesting insight"


async def test_get_nonexistent(db):
    assert await surplus.get_by_id(db, "nope") is None


async def test_list_pending(db):
    await surplus.create(db, id="s2", **_COMMON)
    rows = await surplus.list_pending(db)
    assert any(r["id"] == "s2" for r in rows)


async def test_promote(db):
    await surplus.create(db, id="s3", **_COMMON)
    assert await surplus.promote(db, "s3", promoted_to="procedural_memory") is True
    row = await surplus.get_by_id(db, "s3")
    assert row["promotion_status"] == "promoted"
    assert row["promoted_to"] == "procedural_memory"


async def test_promote_nonexistent(db):
    assert await surplus.promote(db, "nope", promoted_to="x") is False


async def test_discard(db):
    await surplus.create(db, id="s4", **_COMMON)
    assert await surplus.discard(db, "s4") is True
    row = await surplus.get_by_id(db, "s4")
    assert row["promotion_status"] == "discarded"


async def test_discard_nonexistent(db):
    assert await surplus.discard(db, "nope") is False


async def test_promoted_not_in_pending(db):
    await surplus.create(db, id="s5", **_COMMON)
    await surplus.promote(db, "s5", promoted_to="x")
    rows = await surplus.list_pending(db)
    assert all(r["id"] != "s5" for r in rows)


async def test_delete(db):
    await surplus.create(db, id="s6", **_COMMON)
    assert await surplus.delete(db, "s6") is True


async def test_delete_nonexistent(db):
    assert await surplus.delete(db, "nope") is False


async def test_duplicate_id_raises(db):
    await surplus.create(db, id="sdup", **_COMMON)
    with pytest.raises(sqlite3.IntegrityError):
        await surplus.create(db, id="sdup", **_COMMON)
