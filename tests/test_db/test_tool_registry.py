"""Tests for tool_registry CRUD."""

import sqlite3

import pytest

from genesis.db.crud import tool_registry

_COMMON = dict(
    name="bash",
    category="shell",
    description="Execute shell commands",
    tool_type="builtin",
    created_at="2026-01-01T00:00:00",
)


async def test_create_and_get(db):
    rid = await tool_registry.create(db, id="t1", **_COMMON)
    assert rid == "t1"
    row = await tool_registry.get_by_id(db, "t1")
    assert row is not None
    assert row["name"] == "bash"


async def test_get_nonexistent(db):
    assert await tool_registry.get_by_id(db, "nope") is None


async def test_list_by_category(db):
    await tool_registry.create(db, id="t2", **_COMMON)
    await tool_registry.create(db, id="t3", **{**_COMMON, "name": "python", "category": "lang"})
    rows = await tool_registry.list_by_category(db, "shell")
    assert all(r["category"] == "shell" for r in rows)


async def test_list_by_category_empty(db):
    rows = await tool_registry.list_by_category(db, "nonexistent")
    assert rows == []


async def test_list_all(db):
    await tool_registry.create(db, id="t4", **_COMMON)
    rows = await tool_registry.list_all(db)
    assert len(rows) >= 1


async def test_list_all_returns_all(db):
    await tool_registry.create(db, id="t5", **_COMMON)
    await tool_registry.create(db, id="t6", **{**_COMMON, "name": "python", "category": "lang"})
    rows = await tool_registry.list_all(db)
    ids = {r["id"] for r in rows}
    assert "t5" in ids
    assert "t6" in ids


async def test_record_invocation(db):
    await tool_registry.create(db, id="t7", **_COMMON)
    assert await tool_registry.record_invocation(db, "t7", last_used="2026-01-02") is True
    row = await tool_registry.get_by_id(db, "t7")
    assert row["usage_count"] == 1
    assert row["last_used_at"] == "2026-01-02"


async def test_record_invocation_nonexistent(db):
    assert await tool_registry.record_invocation(db, "nope", last_used="x") is False


async def test_update(db):
    await tool_registry.create(db, id="t8", **_COMMON)
    assert await tool_registry.update(db, "t8", description="updated desc") is True
    row = await tool_registry.get_by_id(db, "t8")
    assert row["description"] == "updated desc"


async def test_update_no_fields(db):
    assert await tool_registry.update(db, "t99") is False


async def test_update_nonexistent(db):
    assert await tool_registry.update(db, "nope", name="x") is False


async def test_update_metadata(db):
    await tool_registry.create(db, id="t9", **_COMMON, metadata={"key": "val"})
    await tool_registry.update(db, "t9", metadata={"key": "new"})
    row = await tool_registry.get_by_id(db, "t9")
    assert row is not None


async def test_delete(db):
    await tool_registry.create(db, id="t10", **_COMMON)
    assert await tool_registry.delete(db, "t10") is True


async def test_delete_nonexistent(db):
    assert await tool_registry.delete(db, "nope") is False


async def test_duplicate_id_raises(db):
    await tool_registry.create(db, id="tdup", **_COMMON)
    with pytest.raises(sqlite3.IntegrityError):
        await tool_registry.create(db, id="tdup", **_COMMON)
