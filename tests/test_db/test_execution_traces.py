"""Tests for execution_traces CRUD."""

import sqlite3

import pytest

from genesis.db.crud import execution_traces

_COMMON = dict(
    user_request="deploy app",
    plan=["step1"],
    sub_agents=["agent1"],
    created_at="2026-01-01T00:00:00",
)


async def test_create_and_get(db):
    rid = await execution_traces.create(db, id="e1", **_COMMON)
    assert rid == "e1"
    row = await execution_traces.get_by_id(db, "e1")
    assert row is not None
    assert row["user_request"] == "deploy app"


async def test_get_nonexistent(db):
    assert await execution_traces.get_by_id(db, "nope") is None


async def test_complete(db):
    await execution_traces.create(db, id="e2", **_COMMON)
    assert await execution_traces.complete(
        db, "e2", outcome_class="success", completed_at="2026-01-02",
        quality_gate={"passed": True}, total_cost_usd=0.05,
    ) is True
    row = await execution_traces.get_by_id(db, "e2")
    assert row["outcome_class"] == "success"
    assert row["completed_at"] == "2026-01-02"


async def test_complete_nonexistent(db):
    assert await execution_traces.complete(
        db, "nope", outcome_class="fail", completed_at="x",
    ) is False


async def test_list_by_outcome(db):
    await execution_traces.create(db, id="e3", **_COMMON)
    await execution_traces.complete(db, "e3", outcome_class="success", completed_at="2026-01-02")
    rows = await execution_traces.list_by_outcome(db, "success")
    assert len(rows) >= 1


async def test_list_by_outcome_empty(db):
    rows = await execution_traces.list_by_outcome(db, "nonexistent")
    assert rows == []


async def test_delete(db):
    await execution_traces.create(db, id="e4", **_COMMON)
    assert await execution_traces.delete(db, "e4") is True
    assert await execution_traces.get_by_id(db, "e4") is None


async def test_delete_nonexistent(db):
    assert await execution_traces.delete(db, "nope") is False


async def test_duplicate_id_raises(db):
    await execution_traces.create(db, id="edup", **_COMMON)
    with pytest.raises(sqlite3.IntegrityError):
        await execution_traces.create(db, id="edup", **_COMMON)


# ─── person_id tests ─────────────────────────────────────────────────────────


async def test_person_id_defaults_to_none(db):
    await execution_traces.create(db, id="epid1", **_COMMON)
    row = await execution_traces.get_by_id(db, "epid1")
    assert row["person_id"] is None


async def test_create_with_person_id(db):
    await execution_traces.create(db, id="epid2", person_id="user-42", **_COMMON)
    row = await execution_traces.get_by_id(db, "epid2")
    assert row["person_id"] == "user-42"


async def test_list_by_outcome_filters_by_person_id(db):
    await execution_traces.create(db, id="epid3", person_id="alice", **_COMMON)
    await execution_traces.complete(db, "epid3", outcome_class="success", completed_at="2026-01-02")
    await execution_traces.create(db, id="epid4", person_id="bob", **_COMMON)
    await execution_traces.complete(db, "epid4", outcome_class="success", completed_at="2026-01-02")
    rows = await execution_traces.list_by_outcome(db, "success", person_id="alice")
    assert len(rows) == 1
    assert rows[0]["id"] == "epid3"
