"""Tests for cost_events CRUD."""

import sqlite3

import pytest

from genesis.db.crud import cost_events

_COMMON = dict(
    event_type="llm_call",
    cost_usd=0.05,
    created_at="2026-01-01T00:00:00",
)


async def test_create_and_get(db):
    rid = await cost_events.create(db, id="ce1", **_COMMON)
    assert rid == "ce1"
    row = await cost_events.get_by_id(db, "ce1")
    assert row is not None
    assert row["event_type"] == "llm_call"
    assert row["cost_usd"] == 0.05


async def test_get_nonexistent(db):
    assert await cost_events.get_by_id(db, "nope") is None


async def test_create_with_all_fields(db):
    await cost_events.create(
        db, id="ce2", event_type="llm_call", cost_usd=0.10,
        model="sonnet", provider="anthropic", engine="cloud",
        task_id="task-1", person_id="user-1",
        input_tokens=500, output_tokens=200,
        metadata={"note": "test"}, created_at="2026-01-01T00:00:00",
    )
    row = await cost_events.get_by_id(db, "ce2")
    assert row["model"] == "sonnet"
    assert row["input_tokens"] == 500
    assert row["person_id"] == "user-1"


async def test_query_by_task_id(db):
    await cost_events.create(db, id="ce3", task_id="t1", **_COMMON)
    await cost_events.create(db, id="ce4", task_id="t2", **_COMMON)
    rows = await cost_events.query(db, task_id="t1")
    assert len(rows) == 1
    assert rows[0]["id"] == "ce3"


async def test_query_by_engine(db):
    await cost_events.create(db, id="ce5", engine="ollama", **_COMMON)
    await cost_events.create(db, id="ce6", engine="cloud", **_COMMON)
    rows = await cost_events.query(db, engine="ollama")
    assert len(rows) == 1


async def test_query_by_time_range(db):
    await cost_events.create(
        db, id="ce7", event_type="llm_call", cost_usd=0.01,
        created_at="2026-01-10T00:00:00",
    )
    await cost_events.create(
        db, id="ce8", event_type="llm_call", cost_usd=0.02,
        created_at="2026-01-20T00:00:00",
    )
    rows = await cost_events.query(
        db, since="2026-01-15T00:00:00", until="2026-01-25T00:00:00",
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "ce8"


async def test_sum_cost_by_task(db):
    await cost_events.create(db, id="ce9", task_id="tx", cost_usd=0.10,
                             event_type="llm_call", created_at="2026-01-01T00:00:00")
    await cost_events.create(db, id="ce10", task_id="tx", cost_usd=0.20,
                             event_type="tool_use", created_at="2026-01-01T01:00:00")
    await cost_events.create(db, id="ce11", task_id="other", cost_usd=0.50,
                             event_type="llm_call", created_at="2026-01-01T02:00:00")
    total = await cost_events.sum_cost(db, task_id="tx")
    assert abs(total - 0.30) < 1e-9


async def test_sum_cost_by_period(db):
    await cost_events.create(db, id="ce12", cost_usd=0.10,
                             event_type="llm_call", created_at="2026-02-01T00:00:00")
    await cost_events.create(db, id="ce13", cost_usd=0.20,
                             event_type="llm_call", created_at="2026-02-15T00:00:00")
    await cost_events.create(db, id="ce14", cost_usd=0.50,
                             event_type="llm_call", created_at="2026-03-01T00:00:00")
    total = await cost_events.sum_cost(
        db, since="2026-02-01T00:00:00", until="2026-03-01T00:00:00",
    )
    assert abs(total - 0.30) < 1e-9


async def test_sum_cost_empty(db):
    total = await cost_events.sum_cost(db, task_id="nonexistent")
    assert total == 0.0


async def test_delete(db):
    await cost_events.create(db, id="ce15", **_COMMON)
    assert await cost_events.delete(db, "ce15") is True
    assert await cost_events.get_by_id(db, "ce15") is None


async def test_delete_nonexistent(db):
    assert await cost_events.delete(db, "nope") is False


async def test_duplicate_id_raises(db):
    await cost_events.create(db, id="cedup", **_COMMON)
    with pytest.raises(sqlite3.IntegrityError):
        await cost_events.create(db, id="cedup", **_COMMON)


async def test_invalid_event_type_raises(db):
    with pytest.raises(sqlite3.IntegrityError):
        await cost_events.create(
            db, id="cebad", event_type="INVALID", cost_usd=0.0,
            created_at="2026-01-01",
        )
