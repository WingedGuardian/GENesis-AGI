"""Tests for the follow_up MCP tools — create/update, incl. the tabled lane."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from genesis.db.crud import follow_ups
from genesis.mcp.health import follow_up_tools

pytestmark = pytest.mark.asyncio


async def test_create_default_kind_is_follow_up(db):
    """Without an explicit kind, a create lands in the actionable follow_up lane."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="do the thing", reason="because", strategy="ego_judgment",
        )
    assert "id" in res, res
    assert res["kind"] == "follow_up"
    row = await follow_ups.get_by_id(db, res["id"])
    assert row["kind"] == "follow_up"


async def test_create_kind_tabled(db):
    """kind='tabled' routes a someday/maybe into the tabled lane, off the actionable queue."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="maybe explore idea Y someday", reason="interesting",
            strategy="ego_judgment", kind="tabled",
        )
    assert "id" in res, res
    assert res["kind"] == "tabled"
    row = await follow_ups.get_by_id(db, res["id"])
    assert row["kind"] == "tabled"
    # A tabled item is kept OFF the actionable surface (never auto-actioned),
    # even though its status is the default 'pending'.
    actionable = await follow_ups.get_actionable(db, limit=50)
    assert all(r["id"] != res["id"] for r in actionable)


async def test_create_invalid_kind_errors(db):
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="x", reason="y", strategy="ego_judgment", kind="bogus",
        )
    assert "error" in res
    assert "kind" in res["error"].lower()


async def test_update_relanes_follow_up_to_tabled(db):
    """follow_up_update kind='tabled' demotes an actionable item into the tabled lane."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="reconsider later", reason="low priority", strategy="ego_judgment",
        )
        fid = created["id"]
        res = await follow_up_tools._impl_follow_up_update(fid, kind="tabled")
    assert res.get("kind") == "tabled", res
    row = await follow_ups.get_by_id(db, fid)
    assert row["kind"] == "tabled"


async def test_update_invalid_kind_errors(db):
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="z", reason="z", strategy="ego_judgment",
        )
        res = await follow_up_tools._impl_follow_up_update(created["id"], kind="nope")
    assert "error" in res
    assert "kind" in res["error"].lower()


async def test_list_excludes_tabled_by_default(db):
    """follow_up_list hides tabled items from the agent view and reports them separately."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        await follow_up_tools._impl_follow_up_create(
            content="actionable thing", reason="r", strategy="ego_judgment",
        )
        await follow_up_tools._impl_follow_up_create(
            content="someday idea", reason="r", strategy="ego_judgment", kind="tabled",
        )
        res = await follow_up_tools._impl_follow_up_list()

    kinds = [f["kind"] for f in res["follow_ups"]]
    assert "tabled" not in kinds
    assert res.get("tabled_count") == 1
    # status counts + total reflect actionable items only (the tabled item is pending)
    assert res["total"] == 1


async def test_list_include_tabled_shows_them(db):
    """Opting in surfaces tabled items alongside actionable ones."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        await follow_up_tools._impl_follow_up_create(
            content="actionable thing", reason="r", strategy="ego_judgment",
        )
        await follow_up_tools._impl_follow_up_create(
            content="someday idea", reason="r", strategy="ego_judgment", kind="tabled",
        )
        res = await follow_up_tools._impl_follow_up_list(include_tabled=True)

    kinds = sorted(f["kind"] for f in res["follow_ups"])
    assert kinds == ["follow_up", "tabled"]


async def test_list_status_filter_excludes_tabled(db):
    """The status_filter path (get_by_status) also excludes tabled by default."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        await follow_up_tools._impl_follow_up_create(
            content="actionable pending", reason="r", strategy="ego_judgment",
        )
        # tabled items keep their status ('pending' by default), so a naive
        # status filter would surface them without the kind exclusion.
        await follow_up_tools._impl_follow_up_create(
            content="tabled pending", reason="r", strategy="ego_judgment", kind="tabled",
        )
        res = await follow_up_tools._impl_follow_up_list(status_filter="pending")
    kinds = [f["kind"] for f in res["follow_ups"]]
    assert kinds == ["follow_up"]

    # ...unless the caller opts in.
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res_incl = await follow_up_tools._impl_follow_up_list(
            status_filter="pending", include_tabled=True,
        )
    assert sorted(f["kind"] for f in res_incl["follow_ups"]) == ["follow_up", "tabled"]
