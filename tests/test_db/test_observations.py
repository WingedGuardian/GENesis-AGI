"""Tests for observations CRUD."""

import sqlite3

import pytest

from genesis.db.crud import observations

_COMMON = dict(
    source="sensor",
    type="metric",
    content="cpu at 90%",
    priority="high",
    created_at="2026-01-01T00:00:00",
)


async def test_create_and_get(db):
    rid = await observations.create(db, id="o1", **_COMMON)
    assert rid == "o1"
    row = await observations.get_by_id(db, "o1")
    assert row is not None
    assert row["priority"] == "high"


async def test_get_nonexistent(db):
    assert await observations.get_by_id(db, "nope") is None


async def test_query_no_filters(db):
    await observations.create(db, id="o2", **_COMMON)
    rows = await observations.query(db)
    assert len(rows) >= 1


async def test_query_by_source(db):
    await observations.create(db, id="o3", **_COMMON)
    await observations.create(db, id="o4", **{**_COMMON, "source": "other"})
    rows = await observations.query(db, source="sensor")
    assert all(r["source"] == "sensor" for r in rows)


async def test_query_by_priority(db):
    await observations.create(db, id="o5", **{**_COMMON, "priority": "low"})
    rows = await observations.query(db, priority="low")
    assert all(r["priority"] == "low" for r in rows)


async def test_query_by_resolved(db):
    await observations.create(db, id="o6", **_COMMON)
    rows = await observations.query(db, resolved=False)
    assert all(r["resolved"] == 0 for r in rows)


async def test_resolve(db):
    await observations.create(db, id="o7", **_COMMON)
    assert await observations.resolve(db, "o7", resolved_at="2026-01-02", resolution_notes="fixed") is True
    row = await observations.get_by_id(db, "o7")
    assert row["resolved"] == 1


async def test_resolve_nonexistent(db):
    assert await observations.resolve(db, "nope", resolved_at="x", resolution_notes="x") is False


async def test_increment_retrieved(db):
    await observations.create(db, id="o8", **_COMMON)
    assert await observations.increment_retrieved(db, "o8") is True
    row = await observations.get_by_id(db, "o8")
    assert row["retrieved_count"] == 1


async def test_increment_retrieved_nonexistent(db):
    assert await observations.increment_retrieved(db, "nope") is False


async def test_delete(db):
    await observations.create(db, id="o9", **_COMMON)
    assert await observations.delete(db, "o9") is True
    assert await observations.get_by_id(db, "o9") is None


async def test_delete_nonexistent(db):
    assert await observations.delete(db, "nope") is False


async def test_duplicate_id_raises(db):
    await observations.create(db, id="odup", **_COMMON)
    with pytest.raises(sqlite3.IntegrityError):
        await observations.create(db, id="odup", **_COMMON)


# ─── person_id tests ─────────────────────────────────────────────────────────


async def test_person_id_defaults_to_none(db):
    await observations.create(db, id="opid1", **_COMMON)
    row = await observations.get_by_id(db, "opid1")
    assert row["person_id"] is None


async def test_create_with_person_id(db):
    await observations.create(db, id="opid2", person_id="user-42", **_COMMON)
    row = await observations.get_by_id(db, "opid2")
    assert row["person_id"] == "user-42"


async def test_query_filters_by_person_id(db):
    await observations.create(db, id="opid3", person_id="alice", **_COMMON)
    await observations.create(db, id="opid4", person_id="bob", **_COMMON)
    rows = await observations.query(db, person_id="alice")
    assert len(rows) == 1
    assert rows[0]["id"] == "opid3"


async def test_query_by_category(db):
    await observations.create(db, id="ocat1", category="recon", **_COMMON)
    await observations.create(db, id="ocat2", category="learning", **_COMMON)
    rows = await observations.query(db, category="recon")
    assert len(rows) == 1
    assert rows[0]["id"] == "ocat1"
