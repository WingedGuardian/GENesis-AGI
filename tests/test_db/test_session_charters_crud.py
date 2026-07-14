"""Tests for session_charters/session_ledger CRUD + the charter.md renderer.

The load-bearing invariant under test: origin_prompt/origin_ts are write-once
(import is INSERT OR IGNORE; living-field writers never touch origin columns).
"""

import pytest

from genesis.db.crud import session_charters as crud
from genesis.session_charter import charter_md, write_charter_md

SID = "aaaabbbb-cccc-dddd-eeee-ffff00001111"


# ─── Charter row lifecycle ────────────────────────────────────────────────────


async def test_import_charter_roundtrip(db):
    status = await crud.import_charter(
        db,
        session_id=SID,
        origin_prompt="Let's fix the flaky retry logic in the pipeline.",
        origin_ts="2026-06-30T15:21:47.312Z",
        transcript_path="/tmp/t.jsonl",
        mission="Ship the session manager",
        pointers=["~/.genesis/output/spec.md"],
        compaction_count=3,
        created_at="2026-07-13T00:00:00+00:00",
    )
    assert status == "imported"
    row = await crud.get(db, SID)
    assert row["origin_prompt"] == "Let's fix the flaky retry logic in the pipeline."
    assert row["origin_ts"] == "2026-06-30T15:21:47.312Z"
    assert row["mission"] == "Ship the session manager"
    assert row["pointers"] == ["~/.genesis/output/spec.md"]
    assert row["compaction_count"] == 3


async def test_import_charter_never_overwrites(db):
    await crud.import_charter(db, session_id=SID, origin_prompt="first origin", origin_ts="t1")
    status = await crud.import_charter(
        db, session_id=SID, origin_prompt="SECOND origin", origin_ts="t2"
    )
    assert status == "skipped"
    row = await crud.get(db, SID)
    assert row["origin_prompt"] == "first origin"
    assert row["origin_ts"] == "t1"


async def test_stub_then_import_fills_origin_preserves_living(db):
    """An MCP write before the backfill creates a stub; the import must fill
    the missing origin (else the session loses injection until its next
    compaction) while preserving the living fields (Codex P2, PR #1053)."""
    await crud.upsert_stub(db, SID)
    await crud.set_mission(db, SID, "early mission")
    status = await crud.import_charter(
        db,
        session_id=SID,
        origin_prompt="from backfill",
        origin_ts="t",
        transcript_path="/tmp/legacy.jsonl",
    )
    assert status == "origin_filled"
    row = await crud.get(db, SID)
    assert row["mission"] == "early mission"
    assert row["origin_prompt"] == "from backfill"
    assert row["origin_ts"] == "t"
    assert row["transcript_path"] == "/tmp/legacy.jsonl"


async def test_upsert_stub_idempotent(db):
    await crud.upsert_stub(db, SID)
    await crud.upsert_stub(db, SID)
    row = await crud.get(db, SID)
    assert row["compaction_count"] == 0
    assert row["pointers"] == []


async def test_living_writes_never_touch_origin(db):
    await crud.import_charter(db, session_id=SID, origin_prompt="immutable text", origin_ts="t0")
    await crud.set_mission(db, SID, "new mission")
    await crud.set_pointers(db, SID, ["a", "b"])
    row = await crud.get(db, SID)
    assert row["origin_prompt"] == "immutable text"
    assert row["origin_ts"] == "t0"
    assert row["mission"] == "new mission"
    assert row["pointers"] == ["a", "b"]


async def test_set_pointers_caps(db):
    await crud.upsert_stub(db, SID)
    many = [f"pointer-{i}" for i in range(20)] + ["", "   "]
    long_one = "x" * 500
    await crud.set_pointers(db, SID, [long_one, *many])
    row = await crud.get(db, SID)
    assert len(row["pointers"]) == crud.MAX_POINTERS
    assert row["pointers"][0] == "x" * crud.MAX_POINTER_CHARS
    assert "" not in row["pointers"]


async def test_set_mission_on_missing_row_returns_false(db):
    assert await crud.set_mission(db, "nope-" + SID[:20], "m") is False


# ─── resolve_session_id ───────────────────────────────────────────────────────


async def test_resolve_full_id_passthrough(db):
    assert await crud.resolve_session_id(db, SID) == SID


async def test_resolve_unique_prefix(db):
    await crud.upsert_stub(db, SID)
    assert await crud.resolve_session_id(db, SID[:8]) == SID


async def test_resolve_ambiguous_prefix_unchanged(db):
    await crud.upsert_stub(db, "aaaabbbb-1111-2222-3333-444455556666")
    await crud.upsert_stub(db, "aaaabbbb-9999-8888-7777-666655554444")
    assert await crud.resolve_session_id(db, "aaaabbbb") == "aaaabbbb"


async def test_resolve_prefix_via_cc_sessions_when_uncharted(db):
    """Pre-first-compaction there is no charter row — the resolver must fall
    back to cc_sessions.cc_session_id so a stub is never created under a
    truncated id (Codex P2, PR #1053)."""
    await db.execute(
        "INSERT INTO cc_sessions (id, session_type, model, started_at,"
        " last_activity_at, cc_session_id)"
        " VALUES ('g-1', 'foreground', 'test-model',"
        " '2026-07-13T00:00:00+00:00', '2026-07-13T00:00:00+00:00', ?)",
        (SID,),
    )
    await db.commit()
    assert await crud.resolve_session_id(db, SID[:8]) == SID


# ─── Ledger lifecycle ─────────────────────────────────────────────────────────


async def test_ledger_add_defaults(db):
    item_id = await crud.ledger_add(db, session_id=SID, text="build the thing")
    item = await crud.get_ledger_item(db, item_id)
    assert item["status"] == "open"
    assert item["added_by"] == "foreground"
    assert item["session_id"] == SID
    assert item["evidence"] is None


async def test_ledger_add_invalid_added_by(db):
    with pytest.raises(ValueError, match="added_by"):
        await crud.ledger_add(db, session_id=SID, text="x", added_by="martian")


async def test_ledger_add_empty_text(db):
    with pytest.raises(ValueError, match="non-empty"):
        await crud.ledger_add(db, session_id=SID, text="   ")


async def test_ledger_update_status_and_evidence(db):
    item_id = await crud.ledger_add(db, session_id=SID, text="ship it")
    ok = await crud.ledger_update(db, item_id, status="absorbed", evidence="PR #999")
    assert ok is True
    item = await crud.get_ledger_item(db, item_id)
    assert item["status"] == "absorbed"
    assert item["evidence"] == "PR #999"
    assert item["updated_at"] is not None


async def test_ledger_update_invalid_status(db):
    item_id = await crud.ledger_add(db, session_id=SID, text="x")
    with pytest.raises(ValueError, match="status"):
        await crud.ledger_update(db, item_id, status="finished")


async def test_ledger_update_unknown_id(db):
    assert await crud.ledger_update(db, "deadbeef", status="done") is False


async def test_ledger_list_filter_and_order(db):
    a = await crud.ledger_add(db, session_id=SID, text="first")
    b = await crud.ledger_add(db, session_id=SID, text="second")
    await crud.ledger_update(db, b, status="done")
    open_items = await crud.ledger_list(db, SID, statuses=["open", "in_progress"])
    assert [i["id"] for i in open_items] == [a]
    everything = await crud.ledger_list(db, SID)
    assert {i["id"] for i in everything} == {a, b}


async def test_ledger_list_invalid_filter(db):
    with pytest.raises(ValueError, match="invalid statuses"):
        await crud.ledger_list(db, SID, statuses=["open", "bogus"])


async def test_ledger_counts(db):
    await crud.ledger_add(db, session_id=SID, text="a")
    done = await crud.ledger_add(db, session_id=SID, text="b")
    await crud.ledger_update(db, done, status="done")
    counts = await crud.ledger_counts(db, SID)
    assert counts == {"open": 1, "done": 1}


# ─── Renderer ─────────────────────────────────────────────────────────────────


def test_charter_md_full_render():
    md = charter_md(
        {
            "session_id": SID,
            "origin_ts": "2026-06-30T15:21:47.312Z",
            "origin_prompt": "Original ask.",
            "mission": "Do the work",
            "pointers": ["p1", "p2"],
            "compaction_count": 4,
            "created_at": "2026-07-13T00:00:00+00:00",
        },
        [
            {"text": "open item", "status": "open"},
            {"text": "wip item", "status": "in_progress"},
            {"text": "done item", "status": "done"},
            {"text": "absorbed item", "status": "absorbed"},
            {"text": "dropped item", "status": "dropped"},
        ],
    )
    assert "## Origin (immutable)" in md
    assert "Original ask." in md
    assert "## Mission" in md
    assert "- p1" in md
    assert "- [ ] open item" in md
    assert "- [~] wip item" in md
    assert "- [x] done item" in md
    assert "- [a] absorbed item" in md
    assert "- [d] dropped item" in md


def test_charter_md_stub_row_no_none_string():
    md = charter_md({"session_id": SID, "origin_prompt": None, "compaction_count": 0})
    assert "None" not in md
    assert "## Ledger" not in md


def test_write_charter_md(tmp_path):
    write_charter_md(
        tmp_path,
        SID,
        {"session_id": SID, "origin_prompt": "x", "compaction_count": 1},
        [{"text": "item", "status": "open"}],
    )
    content = (tmp_path / SID / "charter.md").read_text(encoding="utf-8")
    assert "- [ ] item" in content


def test_write_charter_md_swallows_oserror(tmp_path):
    target = tmp_path / "not-a-dir"
    target.write_text("file blocks mkdir")
    # sessions_dir/<sid> collides with an existing FILE → OSError inside; must not raise
    write_charter_md(target / "x", SID, {"session_id": SID}, [])
