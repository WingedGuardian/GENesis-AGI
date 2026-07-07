"""Tests for the build_lane_status MCP tool (read-only lane surface)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import aiosqlite
import pytest

from genesis.db.crud import build_candidates as bc
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def _run(db):
    svc = MagicMock()
    svc._db = db
    with patch("genesis.mcp.health_mcp._service", svc):
        from genesis.mcp.health.build_lane_status import _impl_build_lane_status
        return await _impl_build_lane_status()


@pytest.mark.asyncio
async def test_unavailable_without_db():
    svc = MagicMock()
    svc._db = None
    with patch("genesis.mcp.health_mcp._service", svc):
        from genesis.mcp.health.build_lane_status import _impl_build_lane_status
        result = await _impl_build_lane_status()
    assert result["status"] == "unavailable"


@pytest.mark.asyncio
async def test_reports_prs_declines_greenlights_and_calibration(db):
    # An open (built) draft PR the user approved.
    await bc.create(db, id="c1", item_key="k1", item_title="Dad-joke skill",
                    source_file="f.md", verdict="build")
    await bc.update(db, "c1", outcome="pr_opened",
                    pr_url="https://github.com/o/r/pull/42", branch="task/c1")
    await bc.record_user_decision(db, "c1", user_decision="approved")
    # A decline (reported, never queued).
    await bc.create(db, id="c2", item_key="k2", item_title="Rewrite the kernel",
                    source_file="f.md", verdict="dont_build",
                    verdict_reason="brain-not-body scope")
    # A carded build still awaiting a tap.
    await bc.create(db, id="c3", item_key="k3", item_title="OMI connector",
                    source_file="f.md", verdict="build",
                    approval_request_id="ar-9")

    result = await _run(db)
    assert result["status"] == "ok"
    assert "enabled" in result
    assert result["open_prs"][0]["pr_url"] == "https://github.com/o/r/pull/42"
    assert result["wouldnt_build"][0]["reason"] == "brain-not-body scope"
    assert [g["title"] for g in result["pending_greenlights"]] == ["OMI connector"]
    pairs = {(r["verdict"], r["user_decision"]) for r in result["calibration"]}
    assert ("build", "approved") in pairs
    assert ("dont_build", None) in pairs
