"""Tests for dead_letter CRUD."""

import sqlite3

import pytest

from genesis.db.crud import dead_letter

_COMMON = dict(
    operation_type="llm_call",
    payload='{"messages": []}',
    target_provider="anthropic",
    failure_reason="rate_limited",
    created_at="2026-01-01T00:00:00",
)


async def test_create_and_get(db):
    rid = await dead_letter.create(db, id="dl1", **_COMMON)
    assert rid == "dl1"
    row = await dead_letter.get_by_id(db, "dl1")
    assert row is not None
    assert row["operation_type"] == "llm_call"
    assert row["status"] == "pending"
    assert row["retry_count"] == 0


async def test_get_nonexistent(db):
    assert await dead_letter.get_by_id(db, "nope") is None


async def test_query_pending(db):
    await dead_letter.create(db, id="dl2", **_COMMON)
    await dead_letter.create(db, id="dl3", status="resolved", **_COMMON)
    rows = await dead_letter.query_pending(db)
    assert len(rows) == 1
    assert rows[0]["id"] == "dl2"


async def test_query_pending_by_provider(db):
    await dead_letter.create(db, id="dl4", **_COMMON)
    await dead_letter.create(
        db, id="dl5", operation_type="llm_call", payload="{}",
        target_provider="ollama", failure_reason="timeout",
        created_at="2026-01-01T00:00:00",
    )
    rows = await dead_letter.query_pending(db, target_provider="ollama")
    assert len(rows) == 1
    assert rows[0]["id"] == "dl5"


async def test_update_status(db):
    await dead_letter.create(db, id="dl6", **_COMMON)
    assert await dead_letter.update_status(db, "dl6", status="resolved") is True
    row = await dead_letter.get_by_id(db, "dl6")
    assert row["status"] == "resolved"


async def test_update_status_nonexistent(db):
    assert await dead_letter.update_status(db, "nope", status="resolved") is False


async def test_increment_retry(db):
    await dead_letter.create(db, id="dl7", **_COMMON)
    assert await dead_letter.increment_retry(db, "dl7", last_retry_at="2026-01-02T00:00:00") is True
    row = await dead_letter.get_by_id(db, "dl7")
    assert row["retry_count"] == 1
    assert row["last_retry_at"] == "2026-01-02T00:00:00"
    # increment again
    await dead_letter.increment_retry(db, "dl7", last_retry_at="2026-01-03T00:00:00")
    row = await dead_letter.get_by_id(db, "dl7")
    assert row["retry_count"] == 2


async def test_count_pending(db):
    await dead_letter.create(db, id="dl8", **_COMMON)
    await dead_letter.create(db, id="dl9", **_COMMON)
    await dead_letter.create(db, id="dl10", status="resolved", **_COMMON)
    assert await dead_letter.count_pending(db) == 2


async def test_count_pending_by_provider(db):
    await dead_letter.create(db, id="dl11", **_COMMON)
    await dead_letter.create(
        db, id="dl12", operation_type="llm_call", payload="{}",
        target_provider="ollama", failure_reason="timeout",
        created_at="2026-01-01T00:00:00",
    )
    assert await dead_letter.count_pending(db, target_provider="anthropic") == 1
    assert await dead_letter.count_pending(db, target_provider="ollama") == 1


async def test_delete(db):
    await dead_letter.create(db, id="dl13", **_COMMON)
    assert await dead_letter.delete(db, "dl13") is True
    assert await dead_letter.get_by_id(db, "dl13") is None


async def test_delete_nonexistent(db):
    assert await dead_letter.delete(db, "nope") is False


async def test_duplicate_id_raises(db):
    await dead_letter.create(db, id="dldup", **_COMMON)
    with pytest.raises(sqlite3.IntegrityError):
        await dead_letter.create(db, id="dldup", **_COMMON)
