"""Tests for the ``marketing_prospects_list`` MCP tool — the read-only enumeration
the campaign uses to discover which curated prospects to pitch (→ marketing_send).

Install-agnostic: tmp DB, no network, synthetic prospects only.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

import genesis.mcp.outreach_mcp as om
from genesis.db.crud import marketing_prospects as mp
from genesis.db.schema import create_all_tables

_TS = "2026-09-03T00:00:00"


@pytest.fixture
async def db(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    await conn.commit()
    yield conn
    await conn.close()


@pytest.fixture(autouse=True)
def _wire(db, monkeypatch):
    monkeypatch.setattr(om, "_db", db, raising=False)
    monkeypatch.setattr("genesis.outreach.marketing_config.effective_mode", lambda: "observe")
    yield


async def _list(limit=100):
    tools = await om.mcp.get_tools()
    return json.loads(await tools["marketing_prospects_list"].fn(limit=limit))


@pytest.mark.asyncio
async def test_empty_store_returns_empty_list_with_mode(db):
    out = await _list()
    assert out["status"] == "ok"
    assert out["mode"] == "observe"  # campaign can short-circuit when off
    assert out["count"] == 0
    assert out["prospects"] == []
    assert out["truncated"] is False


@pytest.mark.asyncio
async def test_lists_active_prospects_with_fields(db):
    await mp.create(
        db,
        id="p1",
        email="Dev@Example.com",
        name="Dev One",
        company="Acme",
        created_at=_TS,
        updated_at=_TS,
    )
    out = await _list()
    assert out["count"] == 1
    row = out["prospects"][0]
    assert row == {"id": "p1", "email": "dev@example.com", "name": "Dev One", "company": "Acme"}


@pytest.mark.asyncio
async def test_excludes_opted_out_and_non_active(db):
    await mp.create(db, id="active", email="a@example.com", created_at=_TS, updated_at=_TS)
    await mp.create(db, id="contacted", email="c@example.com", created_at=_TS, updated_at=_TS)
    await mp.mark_contacted(db, "contacted", contacted_at=_TS)  # delivered → excluded
    await mp.create(db, id="opted", email="o@example.com", created_at=_TS, updated_at=_TS)
    await mp.mark_opted_out(db, "opted", opted_out_at=_TS)  # opted-out → excluded
    out = await _list()
    assert [p["id"] for p in out["prospects"]] == ["active"]  # only the active one


@pytest.mark.asyncio
async def test_limit_truncates_and_flags(db):
    for i in range(3):
        await mp.create(db, id=f"p{i}", email=f"p{i}@example.com", created_at=_TS, updated_at=_TS)
    out = await _list(limit=2)
    assert out["count"] == 2
    assert out["total"] == 3  # denominator reported alongside truncated (no silent cap)
    assert out["truncated"] is True


@pytest.mark.asyncio
async def test_off_mode_surfaces_nothing(db, monkeypatch):
    """The outer off-switch: when the marketing lever is off the tool surfaces NO
    prospects (code-gated, mirroring marketing_send's off refusal) — even with a
    populated store, a campaign session that shouldn't be running gets nothing."""
    monkeypatch.setattr("genesis.outreach.marketing_config.effective_mode", lambda: "off")
    await mp.create(db, id="p1", email="a@example.com", created_at=_TS, updated_at=_TS)
    out = await _list()
    assert out["status"] == "ok"
    assert out["mode"] == "off"
    assert out["count"] == 0
    assert out["prospects"] == []


@pytest.mark.asyncio
async def test_no_database_is_clean_error(db, monkeypatch):
    monkeypatch.setattr(om, "_db", None, raising=False)
    out = await _list()
    assert out["status"] == "error"
    assert out["reason"] == "no_database"
    assert out["prospects"] == []
