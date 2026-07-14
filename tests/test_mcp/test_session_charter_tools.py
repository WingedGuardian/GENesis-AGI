"""Tests for the session-charter MCP tools (living fields + ledger).

Invariant under test at the tool layer: origin is not addressable (there is
no parameter that can reach origin_prompt/origin_ts), stubs precede the
first compaction, and every mutation regenerates the charter.md mirror.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from genesis.db.crud import session_charters as crud
from genesis.mcp.health import session_charter_tools as tools

pytestmark = pytest.mark.asyncio

SID = "aaaabbbb-cccc-dddd-eeee-ffff00001111"


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_SESSIONS_DIR", tmp_path)
    return tmp_path


async def test_charter_update_creates_stub_and_sets_mission(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_charter_update(SID, mission="Ship PR-2a")
    assert res.get("updated") == ["mission"], res
    row = await crud.get(db, SID)
    assert row["mission"] == "Ship PR-2a"
    assert row["origin_prompt"] is None  # stub — the hook fills origin later
    md = (sessions_dir / SID / "charter.md").read_text()
    assert "Ship PR-2a" in md


async def test_charter_update_pointer_add_remove_dedup(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        await tools._impl_session_charter_update(SID, add_pointer="a.md")
        await tools._impl_session_charter_update(SID, add_pointer="a.md")
        res = await tools._impl_session_charter_update(SID, add_pointer="b.md")
        assert res["pointers"] == ["a.md", "b.md"]
        res = await tools._impl_session_charter_update(SID, remove_pointer="a.md")
        assert res["pointers"] == ["b.md"]


async def test_charter_update_nothing_to_do(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_charter_update(SID)
    assert "error" in res


async def test_ledger_add_auto_added_by(db, sessions_dir, monkeypatch):
    monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_ledger_add(SID, "build the modal")
    item = await crud.get_ledger_item(db, res["id"])
    assert item["added_by"] == "foreground"
    assert res["open_items"] == 1

    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    with patch.object(tools, "_get_db", return_value=db):
        res2 = await tools._impl_session_ledger_add(SID, "dispatched item")
    item2 = await crud.get_ledger_item(db, res2["id"])
    assert item2["added_by"] == "ambient"


async def test_ledger_add_explicit_invalid_added_by(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_ledger_add(SID, "x", added_by="martian")
    assert "error" in res
    assert "added_by" in res["error"]


async def test_ledger_add_writes_mirror(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        await tools._impl_session_ledger_add(SID, "durable agreement")
    md = (sessions_dir / SID / "charter.md").read_text()
    assert "- [ ] durable agreement" in md


async def test_ledger_update_status_and_mirror(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        created = await tools._impl_session_ledger_add(SID, "close me")
        res = await tools._impl_session_ledger_update(
            created["id"], status="done", evidence="PR #1234"
        )
    assert res["status"] == "done"
    assert res["evidence"] == "PR #1234"
    md = (sessions_dir / SID / "charter.md").read_text()
    assert "- [x] close me" in md


async def test_ledger_update_invalid_status(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        created = await tools._impl_session_ledger_add(SID, "x")
        res = await tools._impl_session_ledger_update(created["id"], status="finished")
    assert "error" in res
    assert "status" in res["error"]


async def test_ledger_update_unknown_id(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_ledger_update("deadbeef", status="done")
    assert "error" in res


async def test_charter_read_with_truncated_sid(db, sessions_dir):
    await crud.import_charter(db, session_id=SID, origin_prompt="the original ask", origin_ts="t0")
    with patch.object(tools, "_get_db", return_value=db):
        await tools._impl_session_ledger_add(SID, "one open thing")
        res = await tools._impl_session_charter(SID[:8])
    assert res["session_id"] == SID
    assert res["origin_prompt"] == "the original ask"
    assert res["ledger_counts"] == {"open": 1}
    assert res["ledger"][0]["text"] == "one open thing"


async def test_charter_read_missing(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        res = await tools._impl_session_charter("nope-1234")
    assert "error" in res


async def test_all_tools_error_when_db_unavailable(sessions_dir):
    with patch.object(tools, "_get_db", return_value=None):
        for res in [
            await tools._impl_session_charter(SID),
            await tools._impl_session_charter_update(SID, mission="m"),
            await tools._impl_session_ledger_add(SID, "x"),
            await tools._impl_session_ledger_update("id1", status="done"),
        ]:
            assert res == {"error": "Database not available"}


async def test_empty_session_id_rejected(db, sessions_dir):
    with patch.object(tools, "_get_db", return_value=db):
        assert "error" in await tools._impl_session_charter("  ")
        assert "error" in await tools._impl_session_charter_update(" ", mission="m")
        assert "error" in await tools._impl_session_ledger_add("", "x")
