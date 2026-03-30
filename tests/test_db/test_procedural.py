"""Tests for procedural_memory CRUD."""

import sqlite3

import pytest

from genesis.db.crud import procedural

_COMMON = dict(
    task_type="deploy",
    principle="automate everything",
    steps=["plan", "execute"],
    tools_used=["bash"],
    context_tags=["infra"],
    created_at="2026-01-01T00:00:00",
)


async def test_create_and_get(db):
    rid = await procedural.create(db, id="p1", **_COMMON)
    assert rid == "p1"
    row = await procedural.get_by_id(db, "p1")
    assert row is not None
    assert row["task_type"] == "deploy"


async def test_get_nonexistent(db):
    assert await procedural.get_by_id(db, "nope") is None


async def test_list_by_task_type(db):
    await procedural.create(db, id="p2", **_COMMON)
    await procedural.create(db, id="p3", **{**_COMMON, "task_type": "test"})
    rows = await procedural.list_by_task_type(db, "deploy")
    assert all(r["task_type"] == "deploy" for r in rows)


async def test_list_by_task_type_empty(db):
    rows = await procedural.list_by_task_type(db, "nonexistent")
    assert rows == []


async def test_update_fields(db):
    await procedural.create(db, id="p4", **_COMMON)
    assert await procedural.update(db, "p4", principle="new principle") is True
    row = await procedural.get_by_id(db, "p4")
    assert row["principle"] == "new principle"


async def test_update_json_field(db):
    await procedural.create(db, id="p5", **_COMMON)
    assert await procedural.update(db, "p5", steps=["a", "b", "c"]) is True


async def test_update_no_fields(db):
    assert await procedural.update(db, "p99") is False


async def test_update_nonexistent(db):
    assert await procedural.update(db, "nope", principle="x") is False


async def test_delete(db):
    await procedural.create(db, id="p6", **_COMMON)
    assert await procedural.delete(db, "p6") is True
    assert await procedural.get_by_id(db, "p6") is None


async def test_delete_nonexistent(db):
    assert await procedural.delete(db, "nope") is False


async def test_duplicate_id_raises(db):
    await procedural.create(db, id="pdup", **_COMMON)
    with pytest.raises(sqlite3.IntegrityError):
        await procedural.create(db, id="pdup", **_COMMON)


# ─── person_id tests ─────────────────────────────────────────────────────────


async def test_person_id_defaults_to_none(db):
    await procedural.create(db, id="ppid1", **_COMMON)
    row = await procedural.get_by_id(db, "ppid1")
    assert row["person_id"] is None


async def test_create_with_person_id(db):
    await procedural.create(db, id="ppid2", person_id="user-42", **_COMMON)
    row = await procedural.get_by_id(db, "ppid2")
    assert row["person_id"] == "user-42"


async def test_list_by_task_type_filters_by_person_id(db):
    await procedural.create(db, id="ppid3", person_id="alice", **_COMMON)
    await procedural.create(db, id="ppid4", person_id="bob", **_COMMON)
    await procedural.create(db, id="ppid5", **_COMMON)  # no person_id
    rows = await procedural.list_by_task_type(db, "deploy", person_id="alice")
    assert len(rows) == 1
    assert rows[0]["id"] == "ppid3"
