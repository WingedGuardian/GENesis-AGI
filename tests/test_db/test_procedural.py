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


async def test_create_with_initial_counts(db):
    """Verify success_count and confidence kwargs are persisted on create."""
    await procedural.create(
        db, id="p_seed", success_count=1, confidence=2 / 3,
        draft=0, activation_tier="LIBRARY", **_COMMON,
    )
    row = await procedural.get_by_id(db, "p_seed")
    assert row is not None
    assert row["success_count"] == 1
    assert abs(row["confidence"] - 2 / 3) < 1e-9
    assert row["draft"] == 0
    assert row["activation_tier"] == "LIBRARY"


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


# ── record_invocation: count reads (procedure recalled = surfaced to model) ──


async def test_record_invocation_increments_and_stamps(db):
    await procedural.create(db, id="p_read", **_COMMON)
    row = await procedural.get_by_id(db, "p_read")
    assert row["invocation_count"] == 0
    assert row["last_used"] is None

    assert await procedural.record_invocation(db, "p_read") is True
    row = await procedural.get_by_id(db, "p_read")
    assert row["invocation_count"] == 1
    assert row["last_used"] is not None

    await procedural.record_invocation(db, "p_read")
    assert (await procedural.get_by_id(db, "p_read"))["invocation_count"] == 2


async def test_record_invocation_unknown_id_is_noop(db):
    assert await procedural.record_invocation(db, "nope") is False


# ── created_session_id: set once at insert, never by an update (#4717) ──


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('{"type": "struggle_extraction", "session_id": "s-1"}', "s-1"),
        ('{"type": "explicit_teach"}', None),
        ('{"session_id": ""}', None),
        ('{"session_id": 7}', None),
        ('["s-1"]', None),
        ("not json", None),
        (None, None),
    ],
)
async def test_create_records_the_source_session_once(db, source, expected):
    await procedural.create(db, id="p_cs", source=source, **_COMMON)
    assert (await procedural.get_by_id(db, "p_cs"))["created_session_id"] == expected


async def test_an_update_never_rewrites_the_creation_session(db):
    await procedural.create(db, id="p_cs", source='{"session_id": "s-1"}', **_COMMON)
    await procedural.update(db, "p_cs", source={"type": "explicit_teach", "session_id": "s-2"})
    assert (await procedural.get_by_id(db, "p_cs"))["created_session_id"] == "s-1"


async def test_upsert_sets_it_on_insert_and_keeps_it_on_conflict(db):
    await procedural.upsert(db, id="p_up", source='{"session_id": "s-1"}', **_COMMON)
    assert (await procedural.get_by_id(db, "p_up"))["created_session_id"] == "s-1"
    await procedural.upsert(db, id="p_up", source='{"session_id": "s-2"}', **_COMMON)
    assert (await procedural.get_by_id(db, "p_up"))["created_session_id"] == "s-1"


@pytest.mark.parametrize("column", ["created_session_id", "created_at"])
async def test_update_refuses_the_creation_columns(db, column):
    """The lesson cap counts creations by these two; an update must never move them."""
    await procedural.create(db, id="p_cs", source='{"session_id": "s-1"}', **_COMMON)
    with pytest.raises(ValueError, match=column):
        await procedural.update(db, "p_cs", **{column: "x"})
    row = await procedural.get_by_id(db, "p_cs")
    assert row["created_session_id"] == "s-1" and row["created_at"] == _COMMON["created_at"]
