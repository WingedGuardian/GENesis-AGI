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
