"""Tests for budgets CRUD."""

import sqlite3

import pytest

from genesis.db.crud import budgets

_COMMON = dict(
    budget_type="daily",
    limit_usd=10.0,
    created_at="2026-01-01T00:00:00",
    updated_at="2026-01-01T00:00:00",
)


async def test_create_and_get(db):
    rid = await budgets.create(db, id="b1", **_COMMON)
    assert rid == "b1"
    row = await budgets.get_by_id(db, "b1")
    assert row is not None
    assert row["budget_type"] == "daily"
    assert row["limit_usd"] == 10.0
    assert row["active"] == 1


async def test_get_nonexistent(db):
    assert await budgets.get_by_id(db, "nope") is None


async def test_list_active(db):
    await budgets.create(db, id="b2", **_COMMON)
    await budgets.create(db, id="b3", budget_type="weekly", limit_usd=50.0,
                         created_at="2026-01-01", updated_at="2026-01-01")
    rows = await budgets.list_active(db)
    assert len(rows) >= 2


async def test_list_active_by_type(db):
    await budgets.create(db, id="b4", **_COMMON)
    await budgets.create(db, id="b5", budget_type="monthly", limit_usd=100.0,
                         created_at="2026-01-01", updated_at="2026-01-01")
    rows = await budgets.list_active(db, budget_type="daily")
    assert all(r["budget_type"] == "daily" for r in rows)


async def test_list_active_excludes_inactive(db):
    await budgets.create(db, id="b6", **_COMMON)
    await budgets.deactivate(db, "b6", updated_at="2026-01-02")
    rows = await budgets.list_active(db)
    assert all(r["id"] != "b6" for r in rows)


async def test_update_limit(db):
    await budgets.create(db, id="b7", **_COMMON)
    assert await budgets.update_limit(db, "b7", limit_usd=25.0, updated_at="2026-01-02") is True
    row = await budgets.get_by_id(db, "b7")
    assert row["limit_usd"] == 25.0
    assert "01-02" in row["updated_at"]


async def test_update_limit_nonexistent(db):
    assert await budgets.update_limit(db, "nope", limit_usd=1.0, updated_at="x") is False


async def test_deactivate(db):
    await budgets.create(db, id="b8", **_COMMON)
    assert await budgets.deactivate(db, "b8", updated_at="2026-01-02") is True
    row = await budgets.get_by_id(db, "b8")
    assert row["active"] == 0


async def test_deactivate_nonexistent(db):
    assert await budgets.deactivate(db, "nope", updated_at="x") is False


async def test_delete(db):
    await budgets.create(db, id="b9", **_COMMON)
    assert await budgets.delete(db, "b9") is True
    assert await budgets.get_by_id(db, "b9") is None


async def test_delete_nonexistent(db):
    assert await budgets.delete(db, "nope") is False


async def test_invalid_budget_type_raises(db):
    with pytest.raises(sqlite3.IntegrityError):
        await budgets.create(
            db, id="bbad", budget_type="INVALID", limit_usd=10.0,
            created_at="2026-01-01", updated_at="2026-01-01",
        )


async def test_person_id_filter(db):
    await budgets.create(db, id="b10", person_id="alice", **_COMMON)
    await budgets.create(db, id="b11", person_id="bob", **_COMMON)
    rows = await budgets.list_active(db, person_id="alice")
    assert len(rows) == 1
    assert rows[0]["id"] == "b10"
