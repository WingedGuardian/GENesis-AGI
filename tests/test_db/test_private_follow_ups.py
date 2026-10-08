"""Private follow-up authority against the real schema and committed proxy."""

import asyncio
import sqlite3
import uuid
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.connection import PendingTransactionError, SerializedConnection
from genesis.db.crud import follow_ups
from genesis.db.schema import create_all_tables, seed_data


async def private(db, **kwargs):
    return await follow_ups.create_private(
        db, request_key=kwargs.pop("request_key", uuid.uuid4().hex), content="Synthetic task", reason="Synthetic evidence", work_state="ready", **kwargs,
    )


async def test_creation_has_no_ambient_authority(db, monkeypatch):
    monkeypatch.setattr(follow_ups, "get_session_id", lambda: "ambient-fixture")
    fid = await private(db)
    row = await follow_ups.get_by_id(db, fid)
    assert row["source"] == "codex_interactive"
    assert row["strategy"] == "user_input_needed"
    assert row["kind"] == "follow_up"
    assert row["pinned"] == 0
    for field in ("source_session", "scheduled_at", "linked_task_id", "goal_id"):
        assert row[field] is None
    assert not db.in_transaction
    legacy = await follow_ups.create(
        db, source="fixture", content="legacy", reason="fixture", strategy="ego_judgment",
    )
    assert (await follow_ups.get_by_id(db, legacy))["source_session"] == "ambient-fixture"
    explicit = await follow_ups.create(
        db, source="fixture", source_session=None, content="legacy", reason="fixture",
        strategy="ego_judgment", scheduled_at="2030-01-01T01:00:00+01:00", pinned=True,
        kind="tabled", revisit_condition="  trigger  ", dedup_key="fixture-key",
    )
    legacy_row = await follow_ups.get_by_id(db, explicit)
    assert legacy_row["source_session"] is None
    assert legacy_row["scheduled_at"] == "2030-01-01T00:00:00+00:00"
    assert legacy_row["pinned"] == 1
    assert legacy_row["revisit_condition"] == "trigger"
    assert legacy_row["dedup_key"] == "fixture-key"


@pytest.mark.parametrize("state,condition,kind,stored", [
    ("ready", "unused", "follow_up", None),
    ("deferred_cold", None, "tabled", None),
    ("blocked_on_trigger", "  trigger  ", "follow_up", "trigger"),
])
async def test_creation_preserves_work_state_contract(db, state, condition, kind, stored):
    fid = await follow_ups.create_private(
        db, request_key="fixture", content="fixture", reason="fixture", work_state=state, revisit_condition=condition,
    )
    row = await follow_ups.get_by_id(db, fid)
    assert (row["kind"], row["revisit_condition"]) == (kind, stored)


@pytest.mark.parametrize("changes", [
    {"content": " "}, {"reason": None}, {"content": "x" * 4001},
    {"work_state": "scheduled"}, {"priority": "urgent"}, {"domain": "foreign"},
    {"work_state": "blocked_on_trigger", "revisit_condition": " "},
])
async def test_invalid_creation_leaves_no_row(db, changes):
    args = dict(request_key="fixture", content="fixture", reason="fixture", work_state="ready") | changes
    with pytest.raises(ValueError):
        await follow_ups.create_private(db, **args)
    async with db.execute("SELECT COUNT(*) FROM follow_ups") as cur:
        assert (await cur.fetchone())[0] == 0


@pytest.mark.parametrize("source", ["claude", "dashboard", "codex_interactive"])
async def test_cross_client_notes_do_not_copy_status_or_provenance(db, source):
    fid = await follow_ups.create(
        db, source=source, source_session="existing-fixture", content="fixture", reason="fixture",
        strategy="user_input_needed",
    )
    await follow_ups.update_status(db, fid, status="completed")
    before = dict(await follow_ups.get_by_id(db, fid))
    assert await follow_ups.update_private(db, fid, resolution_notes="new evidence")
    after = dict(await follow_ups.get_by_id(db, fid))
    assert after == before | {"resolution_notes": "new evidence"}


@pytest.mark.parametrize("column,value", [
    ("strategy", "ego_judgment"), ("scheduled_at", "2030-01-01T00:00:00+00:00"),
    ("linked_task_id", "fixture-task"), ("status", "scheduled"), ("kind", "idea"),
])
async def test_ineligible_rows_are_unchanged(db, column, value):
    fid = await private(db)
    # Disable foreign keys only in this synthetic test to represent a linked
    # record without constructing unrelated task lifecycle state.
    await db.execute("PRAGMA foreign_keys=OFF")
    await db.execute(f"UPDATE follow_ups SET {column} = ? WHERE id = ?", (value, fid))
    await db.commit()
    before = dict(await follow_ups.get_by_id(db, fid))
    assert not await follow_ups.update_private(db, fid, status="completed", resolution_notes="changed")
    assert dict(await follow_ups.get_by_id(db, fid)) == before


@pytest.mark.parametrize("source,status,eligible", [
    (source, status, status not in ("held", "posted"))
    for source in ("board", "follow_up", "codebase")
    for status in ("held", "posted", "rejected", "expired", "dry_run")
])
async def test_promotion_namespaces_and_statuses(db, source, status, eligible):
    fid = await private(db)
    source_ref = "follow_up:" + fid if source == "board" else fid
    await db.execute(
        "INSERT INTO pending_issue_posts "
        "(id,request_id,repo,title,body,source,source_ref,cell_domain,cell_verb,cell_risk_class,held_at,status) "
        "VALUES ('fixture','fixture','owner/repo','fixture','fixture',?,?,"
        "'github','issue_create','bulk','2030-01-01',?)", (source, source_ref, status),
    )
    await db.commit()
    assert await follow_ups.update_private(db, fid, resolution_notes="changed") is eligible
    row = await follow_ups.get_by_id(db, fid)
    assert row["resolution_notes"] == ("changed" if eligible else None)


async def test_board_link_blocks_updates(db):
    fid = await private(db)
    await db.execute(
        "INSERT INTO board_links (id,source_kind,source_id,repo,issue_number,promoted_by,"
        "scan_receipt,body_sha256,created_at,updated_at) "
        "VALUES ('fixture','follow_up',?,'owner/repo',1,'fixture','fixture','fixture','fixture','fixture')",
        (fid,),
    )
    await db.commit()
    assert not await follow_ups.update_private(db, fid, resolution_notes="changed")


@pytest.mark.parametrize("terminal", ["completed", "failed"])
async def test_pin_blocks_terminal_but_preserves_note_authority(db, terminal):
    fid = await private(db)
    await follow_ups.set_pinned(db, fid, True)
    assert not await follow_ups.update_private(db, fid, status=terminal, priority="critical")
    assert await follow_ups.update_private(db, fid, resolution_notes="fixture")
    row = await follow_ups.get_by_id(db, fid)
    assert (row["status"], row["priority"], row["pinned"]) == ("pending", "medium", 1)


async def test_terminal_timestamp_idempotence_and_reopen(db):
    fid = await private(db)
    assert await follow_ups.update_private(db, fid, status="completed")
    stamp = (await follow_ups.get_by_id(db, fid))["completed_at"]
    assert stamp
    assert await follow_ups.update_private(db, fid, status="completed")
    assert (await follow_ups.get_by_id(db, fid))["completed_at"] == stamp
    assert await follow_ups.update_private(db, fid, blocked_reason="fixture")
    row = await follow_ups.get_by_id(db, fid)
    assert row["status"] == "blocked" and row["completed_at"] is None


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_foreign_pending_transaction_is_not_committed(db, operation):
    fid = await private(db)
    await db.execute("UPDATE follow_ups SET priority='high' WHERE id=?", (fid,))
    with pytest.raises(PendingTransactionError):
        if operation == "create":
            await private(db)
        else:
            await follow_ups.update_private(db, fid, resolution_notes="changed")
    assert db.in_transaction
    await db.rollback()
    assert (await follow_ups.get_by_id(db, fid))["priority"] == "medium"


async def test_commit_failure_rolls_back_owned_update(db, monkeypatch):
    fid = await private(db)
    monkeypatch.setattr(db._conn, "commit", AsyncMock(side_effect=RuntimeError("synthetic commit failure")))
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        await follow_ups.update_private(db, fid, status="completed", resolution_notes="changed")
    row = await follow_ups.get_by_id(db, fid)
    assert row["status"] == "pending" and row["resolution_notes"] is None
    assert not db.in_transaction


async def test_missing_classification_table_fails_closed(db):
    fid = await private(db)
    await db.execute("DROP TABLE board_links")
    await db.commit()
    with pytest.raises(sqlite3.OperationalError):
        await follow_ups.update_private(db, fid, resolution_notes="changed")
    assert (await follow_ups.get_by_id(db, fid))["resolution_notes"] is None


@pytest.fixture
async def independent_connections(tmp_path):
    path = tmp_path / "private-follow-ups.sqlite"
    first = await aiosqlite.connect(path)
    first.row_factory = aiosqlite.Row
    await create_all_tables(first)
    await seed_data(first)
    await first.commit()
    second = await aiosqlite.connect(path)
    second.row_factory = aiosqlite.Row
    clients = SerializedConnection(first), SerializedConnection(second)
    try:
        yield clients
    finally:
        await clients[0].close()
        await clients[1].close()


@pytest.mark.parametrize("mutation", ["schedule", "pin", "complete"])
async def test_other_connection_change_at_write_boundary(independent_connections, monkeypatch, mutation):
    writer, other = independent_connections
    fid = await private(writer)
    original = SerializedConnection.execute_committed
    entered = False

    async def changed_before_write(client, sql, params):
        nonlocal entered
        if client is not writer:
            return await original(client, sql, params)
        entered = True
        if mutation == "schedule":
            await other.execute_committed(
                "UPDATE follow_ups SET scheduled_at='2030-01-01' WHERE id=?", (fid,),
            )
        elif mutation == "pin":
            await follow_ups.set_pinned(other, fid, True)
        else:
            await follow_ups.update_status(other, fid, status="completed")
        return await original(client, sql, params)

    monkeypatch.setattr(SerializedConnection, "execute_committed", changed_before_write)
    if mutation == "complete":
        assert await follow_ups.update_private(writer, fid, resolution_notes="new evidence")
        row = await follow_ups.get_by_id(other, fid)
        assert row["status"] == "completed" and row["completed_at"]
        assert row["resolution_notes"] == "new evidence"
    else:
        assert not await follow_ups.update_private(writer, fid, status="failed", resolution_notes="changed")
        row = await follow_ups.get_by_id(other, fid)
        assert row["status"] == "pending" and row["resolution_notes"] is None
        assert row["scheduled_at"] == ("2030-01-01" if mutation == "schedule" else None)
        assert row["pinned"] == (1 if mutation == "pin" else 0)
    assert entered


@pytest.mark.parametrize("key", ["", " ", "a.b", "a/b", "é", "a" * 129, None])
async def test_request_key_is_bounded_opaque_ascii(db, key):
    with pytest.raises(ValueError, match="request key"):
        await private(db, request_key=key)
    async with db.execute("SELECT COUNT(*) FROM follow_ups") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_replay_preserves_other_clients_mutations_and_nullable_fields(db):
    fid = await private(db, request_key="same-request")
    await follow_ups.update_status(db, fid, status="completed")
    await follow_ups.update_private(db, fid, resolution_notes="other client evidence")
    await follow_ups.set_pinned(db, fid, True)
    before = await follow_ups.get_by_id(db, fid)
    assert await private(db, request_key="same-request") == fid
    assert await follow_ups.get_by_id(db, fid) == before
    assert before["dedup_key"] == "codex_private:same-request"


@pytest.mark.parametrize("change", [{"priority": "high"}, {"domain": "internal"},
                                     {"revisit_condition": "trigger", "work_state": "blocked_on_trigger"}])
async def test_same_key_changed_request_refuses_without_duplicate(db, change):
    args = dict(request_key="same", content="fixture", reason="fixture", work_state="ready")
    fid = await follow_ups.create_private(db, **args)
    before = await follow_ups.get_by_id(db, fid)
    with pytest.raises(RuntimeError, match="cannot be replayed"):
        await follow_ups.create_private(db, **(args | change))
    assert await follow_ups.get_by_id(db, fid) == before
    async with db.execute("SELECT COUNT(*) FROM follow_ups") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_legacy_namespace_collision_is_not_adopted(db):
    fid = await follow_ups.create(db, source="claude", strategy="user_input_needed",
                                 content="Synthetic task", reason="Synthetic evidence",
                                 dedup_key="codex_private:same")
    before = await follow_ups.get_by_id(db, fid)
    with pytest.raises(RuntimeError, match="cannot be replayed"):
        await private(db, request_key="same")
    assert await follow_ups.get_by_id(db, fid) == before


async def test_scheduled_replay_refuses_without_clobber(db):
    fid = await private(db, request_key="same")
    await db.execute_committed("UPDATE follow_ups SET scheduled_at='2030-01-01' WHERE id=?", (fid,))
    before = await follow_ups.get_by_id(db, fid)
    with pytest.raises(RuntimeError, match="cannot be replayed"):
        await private(db, request_key="same")
    assert await follow_ups.get_by_id(db, fid) == before


async def test_two_connections_converge_on_one_request(independent_connections):
    first, second = independent_connections
    ids = await asyncio.gather(private(first, request_key="race"), private(second, request_key="race"))
    assert ids[0] == ids[1]
    async with first.execute("SELECT COUNT(*) FROM follow_ups") as cur:
        assert (await cur.fetchone())[0] == 1


async def test_cancel_after_persisted_commit_retries_same_record(db, monkeypatch):
    original = SerializedConnection.execute_committed
    persisted = False

    async def commit_then_cancel(client, sql, params):
        nonlocal persisted
        result = await original(client, sql, params)
        if client is db and not persisted:
            persisted = True
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(SerializedConnection, "execute_committed", commit_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        await private(db, request_key="cancelled-request")
    assert persisted and not db.in_transaction
    fid = await private(db, request_key="cancelled-request")
    async with db.execute("SELECT id FROM follow_ups") as cur:
        assert [row[0] for row in await cur.fetchall()] == [fid]


@pytest.mark.parametrize("status", [None, "blocked"])
async def test_blank_blocking_reason_refuses_but_explicit_nonblocked_can_clear(db, status):
    fid = await private(db)
    with pytest.raises(ValueError, match="meaningful reason"):
        await follow_ups.update_private(db, fid, status=status, blocked_reason=" ")
    assert (await follow_ups.get_by_id(db, fid))["status"] == "pending"
    assert await follow_ups.update_private(db, fid, status="in_progress", blocked_reason="")
