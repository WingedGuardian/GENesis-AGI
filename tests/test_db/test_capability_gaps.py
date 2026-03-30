"""Tests for capability_gaps CRUD."""

import sqlite3

import pytest

from genesis.db.crud import capability_gaps

_COMMON = dict(
    description="cannot parse PDF",
    gap_type="capability_gap",
    first_seen="2026-01-01",
    last_seen="2026-01-01",
)


async def test_create_and_get(db):
    rid = await capability_gaps.create(db, id="g1", **_COMMON)
    assert rid == "g1"
    row = await capability_gaps.get_by_id(db, "g1")
    assert row is not None
    assert row["description"] == "cannot parse PDF"


async def test_get_nonexistent(db):
    assert await capability_gaps.get_by_id(db, "nope") is None


async def test_list_open(db):
    await capability_gaps.create(db, id="g2", **_COMMON)
    rows = await capability_gaps.list_open(db)
    assert any(r["id"] == "g2" for r in rows)


async def test_increment_frequency(db):
    await capability_gaps.create(db, id="g3", **_COMMON)
    assert await capability_gaps.increment_frequency(db, "g3", last_seen="2026-01-02") is True
    row = await capability_gaps.get_by_id(db, "g3")
    assert row["frequency"] == 2
    assert row["last_seen"] == "2026-01-02"


async def test_increment_frequency_nonexistent(db):
    assert await capability_gaps.increment_frequency(db, "nope", last_seen="x") is False


async def test_resolve(db):
    await capability_gaps.create(db, id="g4", **_COMMON)
    assert await capability_gaps.resolve(db, "g4", resolved_at="2026-01-02", resolution_notes="added tool") is True
    row = await capability_gaps.get_by_id(db, "g4")
    assert row["status"] == "resolved"


async def test_resolve_nonexistent(db):
    assert await capability_gaps.resolve(db, "nope", resolved_at="x", resolution_notes="x") is False


async def test_resolved_not_in_open(db):
    await capability_gaps.create(db, id="g5", **_COMMON)
    await capability_gaps.resolve(db, "g5", resolved_at="2026-01-02", resolution_notes="done")
    rows = await capability_gaps.list_open(db)
    assert all(r["id"] != "g5" for r in rows)


async def test_delete(db):
    await capability_gaps.create(db, id="g6", **_COMMON)
    assert await capability_gaps.delete(db, "g6") is True


async def test_delete_nonexistent(db):
    assert await capability_gaps.delete(db, "nope") is False


async def test_duplicate_id_raises(db):
    await capability_gaps.create(db, id="gdup", **_COMMON)
    with pytest.raises(sqlite3.IntegrityError):
        await capability_gaps.create(db, id="gdup", **_COMMON)
