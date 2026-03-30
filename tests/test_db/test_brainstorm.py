"""Tests for brainstorm_log CRUD."""

import sqlite3

import pytest

from genesis.db.crud import brainstorm

_COMMON = dict(
    session_type="upgrade_user",
    model_used="opus",
    outputs=["idea1", "idea2"],
    created_at="2026-01-01T00:00:00",
)


async def test_create_and_get(db):
    rid = await brainstorm.create(db, id="b1", **_COMMON)
    assert rid == "b1"
    row = await brainstorm.get_by_id(db, "b1")
    assert row is not None
    assert row["session_type"] == "upgrade_user"


async def test_get_nonexistent(db):
    assert await brainstorm.get_by_id(db, "nope") is None


async def test_list_by_type(db):
    await brainstorm.create(db, id="b2", **_COMMON)
    await brainstorm.create(db, id="b3", **{**_COMMON, "session_type": "upgrade_self"})
    rows = await brainstorm.list_by_type(db, "upgrade_user")
    assert all(r["session_type"] == "upgrade_user" for r in rows)


async def test_list_by_type_empty(db):
    rows = await brainstorm.list_by_type(db, "nonexistent")
    assert rows == []


async def test_update_counts(db):
    await brainstorm.create(db, id="b4", **_COMMON)
    assert await brainstorm.update_counts(db, "b4", promoted_count=2, discarded_count=1) is True
    row = await brainstorm.get_by_id(db, "b4")
    assert row["promoted_count"] == 2
    assert row["discarded_count"] == 1


async def test_update_counts_nonexistent(db):
    assert await brainstorm.update_counts(db, "nope", promoted_count=0, discarded_count=0) is False


async def test_create_with_staging_ids(db):
    rid = await brainstorm.create(db, id="b5", staging_ids=["s1", "s2"], **_COMMON)
    row = await brainstorm.get_by_id(db, rid)
    assert row is not None


async def test_delete(db):
    await brainstorm.create(db, id="b6", **_COMMON)
    assert await brainstorm.delete(db, "b6") is True


async def test_delete_nonexistent(db):
    assert await brainstorm.delete(db, "nope") is False


async def test_duplicate_id_raises(db):
    await brainstorm.create(db, id="bdup", **_COMMON)
    with pytest.raises(sqlite3.IntegrityError):
        await brainstorm.create(db, id="bdup", **_COMMON)
