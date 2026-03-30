"""Tests for outreach_history CRUD."""

import sqlite3

import pytest

from genesis.db.crud import outreach

_COMMON = dict(
    signal_type="news",
    topic="AI updates",
    category="finding",
    salience_score=0.8,
    channel="discord",
    message_content="Check this out",
    created_at="2026-01-01T00:00:00",
)


async def test_create_and_get(db):
    rid = await outreach.create(db, id="oh1", **_COMMON)
    assert rid == "oh1"
    row = await outreach.get_by_id(db, "oh1")
    assert row is not None
    assert row["channel"] == "discord"


async def test_get_nonexistent(db):
    assert await outreach.get_by_id(db, "nope") is None


async def test_list_by_channel(db):
    await outreach.create(db, id="oh2", **_COMMON)
    await outreach.create(db, id="oh3", **{**_COMMON, "channel": "slack"})
    rows = await outreach.list_by_channel(db, "discord")
    assert all(r["channel"] == "discord" for r in rows)


async def test_list_by_channel_empty(db):
    rows = await outreach.list_by_channel(db, "nonexistent")
    assert rows == []


async def test_record_engagement(db):
    await outreach.create(db, id="oh4", **_COMMON)
    assert await outreach.record_engagement(
        db, "oh4", engagement_outcome="positive",
        engagement_signal="liked", prediction_error=0.1,
    ) is True
    row = await outreach.get_by_id(db, "oh4")
    assert row["engagement_outcome"] == "positive"


async def test_record_engagement_nonexistent(db):
    assert await outreach.record_engagement(db, "nope", engagement_outcome="x") is False


async def test_record_delivery(db):
    await outreach.create(db, id="oh5", **_COMMON)
    assert await outreach.record_delivery(db, "oh5", delivered_at="2026-01-02") is True
    row = await outreach.get_by_id(db, "oh5")
    assert row["delivered_at"] == "2026-01-02"


async def test_record_delivery_nonexistent(db):
    assert await outreach.record_delivery(db, "nope", delivered_at="x") is False


async def test_delete(db):
    await outreach.create(db, id="oh6", **_COMMON)
    assert await outreach.delete(db, "oh6") is True


async def test_delete_nonexistent(db):
    assert await outreach.delete(db, "nope") is False


async def test_duplicate_id_raises(db):
    await outreach.create(db, id="ohdup", **_COMMON)
    with pytest.raises(sqlite3.IntegrityError):
        await outreach.create(db, id="ohdup", **_COMMON)


# ─── person_id tests ─────────────────────────────────────────────────────────


async def test_person_id_defaults_to_none(db):
    await outreach.create(db, id="ohpid1", **_COMMON)
    row = await outreach.get_by_id(db, "ohpid1")
    assert row["person_id"] is None


async def test_create_with_person_id(db):
    await outreach.create(db, id="ohpid2", person_id="user-42", **_COMMON)
    row = await outreach.get_by_id(db, "ohpid2")
    assert row["person_id"] == "user-42"


async def test_list_by_channel_filters_by_person_id(db):
    await outreach.create(db, id="ohpid3", person_id="alice", **_COMMON)
    await outreach.create(db, id="ohpid4", person_id="bob", **_COMMON)
    rows = await outreach.list_by_channel(db, "discord", person_id="alice")
    assert len(rows) == 1
    assert rows[0]["id"] == "ohpid3"
