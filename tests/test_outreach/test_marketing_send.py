"""Tests for the ``marketing_send`` MCP tool — the autonomous cold-marketing
dispatch that resolves its recipient IN CODE from ``marketing_prospects`` and
NEVER accepts a raw address.

Install-agnostic: tmp DB, no live pipeline (subprocess/enqueue path), no
network, synthetic prospects only.  Covers:
  (g) absent / opted-out prospect_id → refuses, enqueues NOTHING.
  (h) empty store + no adapter → clean refuse, no crash.
  happy path → enqueues to pending_outreach with the resolved recipient,
      labeled_surplus=True, and the cold-send category.
  lever `off` → refuses regardless of a valid prospect.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

import genesis.mcp.outreach_mcp as om
from genesis.db.crud import marketing_prospects as mp
from genesis.db.crud import pending_outreach as po
from genesis.db.schema import create_all_tables

_TS = "2026-08-25T00:00:00"


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
    """Wire the module globals for the subprocess (pipeline=None) path and force
    the lever ON (mode=live) unless a test overrides it."""
    monkeypatch.setattr(om, "_db", db, raising=False)
    monkeypatch.setattr(om, "_pipeline", None, raising=False)
    monkeypatch.setattr("genesis.outreach.marketing_config.effective_mode", lambda: "live")
    yield


async def _call(prospect_id, subject="Hello", body="World"):
    tools = await om.mcp.get_tools()
    return json.loads(
        await tools["marketing_send"].fn(
            prospect_id=prospect_id,
            subject=subject,
            body=body,
        )
    )


@pytest.mark.asyncio
async def test_absent_prospect_refuses_and_enqueues_nothing(db):
    out = await _call("nope")
    assert out["status"] == "refused"
    assert await po.drain(db, now="2099-01-01T00:00:00") == []  # nothing queued


@pytest.mark.asyncio
async def test_opted_out_prospect_refuses(db):
    await mp.create(db, id="p1", email="a@example.com", created_at=_TS, updated_at=_TS)
    await mp.mark_opted_out(db, "p1", opted_out_at=_TS)
    out = await _call("p1")
    assert out["status"] == "refused"
    assert "opted_out" in out["reason"]
    assert await po.drain(db, now="2099-01-01T00:00:00") == []


@pytest.mark.asyncio
async def test_blank_or_invalid_email_refuses(db):
    await mp.create(db, id="blank", email="   ", created_at=_TS, updated_at=_TS)
    await mp.create(db, id="bad", email="not-an-email", created_at=_TS, updated_at=_TS)
    assert (await _call("blank"))["status"] == "refused"
    assert (await _call("bad"))["status"] == "refused"
    assert await po.drain(db, now="2099-01-01T00:00:00") == []


@pytest.mark.asyncio
async def test_lever_off_refuses(db, monkeypatch):
    monkeypatch.setattr("genesis.outreach.marketing_config.effective_mode", lambda: "off")
    await mp.create(db, id="p1", email="a@example.com", created_at=_TS, updated_at=_TS)
    out = await _call("p1")
    assert out["status"] == "refused"
    assert await po.drain(db, now="2099-01-01T00:00:00") == []


@pytest.mark.asyncio
async def test_happy_path_enqueues_code_resolved_recipient_labeled_bulk(db):
    await mp.create(db, id="p1", email="prospect@example.com", created_at=_TS, updated_at=_TS)
    out = await _call("p1", subject="Try Genesis", body="It remembers everything.")
    assert out["status"] == "queued"

    rows = await po.drain(db, now="2099-01-01T00:00:00")
    assert len(rows) == 1
    row = rows[0]
    assert row["channel"] == "email"
    assert row["validated_recipient"] == "prospect@example.com"  # resolved in CODE
    assert row["labeled_surplus"] == 1  # → classifies BULK downstream
    assert "Try Genesis" in row["message"]


@pytest.mark.asyncio
async def test_empty_store_no_adapter_is_clean_noop(db):
    # Fresh clone: no adapter (pipeline=None) and an empty prospect table.
    out = await _call("anything")
    assert out["status"] == "refused"  # clean refuse, no crash
    assert await po.drain(db, now="2099-01-01T00:00:00") == []
