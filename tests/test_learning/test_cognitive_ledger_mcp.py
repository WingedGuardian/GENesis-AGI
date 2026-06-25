"""Tests for the cognitive-ledger MCP tools (operator surface).

Exercises the _impl functions directly against a stubbed HealthDataService,
mirroring the self_improvement_status / ego_calibration MCP surface tests.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.learning.cognitive_ledger import record_file_modification
from genesis.mcp.health.cognitive_ledger_tools import (
    _impl_cognitive_modification_rollback,
    _impl_cognitive_modification_status,
)


@pytest.fixture
async def db(tmp_path):
    from genesis.db.schema import create_all_tables

    path = str(tmp_path / "cl.db")
    async with aiosqlite.connect(path) as conn:
        await create_all_tables(conn)
        await conn.commit()
        yield conn


class _StubService:
    def __init__(self, _db):
        self._db = _db


@pytest.mark.asyncio
async def test_unavailable_when_no_service(monkeypatch):
    import genesis.mcp.health_mcp as hm

    monkeypatch.setattr(hm, "_service", None, raising=False)
    assert (await _impl_cognitive_modification_status())["status"] == "unavailable"
    rb = await _impl_cognitive_modification_rollback("x")
    assert rb["ok"] is False


@pytest.mark.asyncio
async def test_empty_status_is_coherent(db, monkeypatch):
    import genesis.mcp.health_mcp as hm

    monkeypatch.setattr(hm, "_service", _StubService(db), raising=False)
    res = await _impl_cognitive_modification_status()
    assert res["status"] == "ok"
    assert res["total_targets"] == 0
    assert res["recent"] == []


@pytest.mark.asyncio
async def test_status_lists_then_rollback_reverts(db, tmp_path, monkeypatch):
    import genesis.mcp.health_mcp as hm

    monkeypatch.setattr(hm, "_service", _StubService(db), raising=False)

    p = tmp_path / "SKILL.md"
    p.write_text("v1")
    mid = await record_file_modification(
        db, actor="skill_evolution", path=p, new_content="v2",
        summary="auto-apply",
    )

    status = await _impl_cognitive_modification_status()
    assert status["status"] == "ok"
    assert status["total_targets"] == 1
    listed = {r["mod_id"]: r for r in status["recent"]}
    assert mid in listed
    assert listed[mid]["actor"] == "skill_evolution"
    assert listed[mid]["prior_chars"] == 2  # "v1"

    rb = await _impl_cognitive_modification_rollback(mid)
    assert rb["ok"] is True
    assert p.read_text() == "v1"

    # Filter by actor works too.
    only = await _impl_cognitive_modification_status(actor="triage_calibration_daily")
    assert only["recent"] == []
