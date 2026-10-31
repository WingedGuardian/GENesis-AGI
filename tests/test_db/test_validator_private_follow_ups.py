"""Private eligibility against the actual schema, with no public/task authority."""

import sqlite3

import pytest

from genesis.db.crud import follow_ups


async def private(db, key="owned-request", **changes):
    create = getattr(follow_ups, "create_private", None)
    assert callable(create), "private creation boundary is not implemented"
    return await create(db, **(dict(
        request_key=key, content="Synthetic task", reason="Synthetic evidence", work_state="ready",
    ) | changes))


async def test_private_create_replay_preserves_current_state(db, monkeypatch):
    monkeypatch.setattr(follow_ups, "get_session_id", lambda: "unrelated-session")
    fid = await private(db)
    row = await follow_ups.get_by_id(db, fid)
    assert (row["source"], row["source_session"], row["strategy"]) == (
        "codex_interactive", None, "user_input_needed",
    )
    for field in ("scheduled_at", "linked_task_id", "goal_id"):
        assert row[field] is None
    assert row["pinned"] == 0
    await follow_ups.update_status(db, fid, status="completed")
    before = await follow_ups.get_by_id(db, fid)
    assert await private(db) == fid
    assert await follow_ups.get_by_id(db, fid) == before


@pytest.mark.parametrize("table", ["board_links", "pending_issue_posts"])
async def test_private_create_missing_predicate_table_never_inserts(db, table):
    await db.execute(f"DROP TABLE {table}")
    await db.commit()
    with pytest.raises(sqlite3.OperationalError):
        await private(db)
    async with db.execute("SELECT COUNT(*) FROM follow_ups") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_private_notes_preserve_other_clients_status_and_pin(db):
    fid = await follow_ups.create(
        db, source="foreground_session", source_session="existing-owner",
        content="Synthetic task", strategy="user_input_needed",
    )
    await follow_ups.update_status(db, fid, status="completed")
    await follow_ups.set_pinned(db, fid, True)
    update = getattr(follow_ups, "update_private", None)
    assert callable(update), "conditional private update is not implemented"
    before = await follow_ups.get_by_id(db, fid)
    assert not await update(db, fid, status="failed")
    assert await update(db, fid, resolution_notes="Synthetic measurement")
    assert await follow_ups.get_by_id(db, fid) == before | {
        "resolution_notes": "Synthetic measurement",
    }


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


@pytest.mark.parametrize("status", [None, "blocked"])
async def test_blank_blocking_reason_refuses_but_explicit_nonblocked_can_clear(db, status):
    fid = await private(db)
    with pytest.raises(ValueError, match="meaningful reason"):
        await follow_ups.update_private(db, fid, status=status, blocked_reason=" ")
    assert (await follow_ups.get_by_id(db, fid))["status"] == "pending"
    assert await follow_ups.update_private(db, fid, status="in_progress", blocked_reason="")
