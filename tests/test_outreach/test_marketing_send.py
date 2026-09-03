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
from genesis.db.crud import pending_email_sends as pes
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


# --- PR2: contact-stamping (loop-fix) + approval-flood guard -----------------


@pytest.mark.asyncio
async def test_stage_does_not_stamp_at_enqueue(db):
    """The subprocess (campaign) path stages to pending_outreach but does NOT stamp
    the prospect at enqueue — 'contacted' is applied downstream by the email-gate
    drain on a CONFIRMED outcome (delivered / owner-rejected), so a send dropped
    before it reaches a decision never burns the prospect. After staging the
    prospect is still 'active' (see test_email_gate_watcher for the drain stamp)."""
    await mp.create(db, id="p1", email="prospect@example.com", created_at=_TS, updated_at=_TS)
    out = await _call("p1")
    assert out["status"] == "queued"
    row = await mp.get_by_id(db, "p1")
    assert row["status"] == "active"  # NOT stamped at stage
    assert row["last_contacted_at"] is None
    assert len(await mp.list_active(db)) == 1  # still eligible until an outcome confirms


@pytest.mark.asyncio
async def test_already_contacted_prospect_refuses_and_enqueues_nothing(db):
    """A non-active prospect (already staged/contacted) is refused — no duplicate
    hold across ticks, no re-pitch."""
    await mp.create(db, id="p1", email="a@example.com", created_at=_TS, updated_at=_TS)
    await mp.mark_contacted(db, "p1", contacted_at=_TS)
    out = await _call("p1")
    assert out["status"] == "refused"
    assert out["reason"] == "already_contacted"
    assert await po.drain(db, now="2099-01-01T00:00:00") == []


@pytest.mark.asyncio
async def test_replied_prospect_is_never_recold_pitched(db):
    """A prospect already in conversation (status='replied') is not re-cold-pitched."""
    await mp.create(db, id="p1", email="a@example.com", created_at=_TS, updated_at=_TS)
    await mp.mark_contacted(db, "p1", contacted_at=_TS, status="replied")
    out = await _call("p1")
    assert out["status"] == "refused"
    assert out["reason"] == "already_contacted"


@pytest.mark.asyncio
async def test_flood_cap_refuses_when_approval_queue_full(db, monkeypatch):
    """When >= max_pending_holds BULK sends already await owner approval, a new
    stage is refused (the ASK-state approval-flood guard): nothing enqueued, and
    the prospect is NOT stamped (a refused send is a no-op)."""
    monkeypatch.setattr("genesis.outreach.marketing_config.max_pending_holds", lambda: 2)
    for i in range(2):
        await pes.create(
            db,
            id=f"h{i}",
            request_id=f"r{i}",
            validated_recipient=f"held{i}@example.com",
            category="notification",
            message="held pitch",
            cell_domain="email",
            cell_verb="send",
            cell_risk_class="bulk",
            held_at=_TS,
        )
    await mp.create(db, id="p1", email="a@example.com", created_at=_TS, updated_at=_TS)
    out = await _call("p1")
    assert out["status"] == "refused"
    assert out["reason"] == "approval_queue_full"
    assert await po.drain(db, now="2099-01-01T00:00:00") == []
    assert (await mp.get_by_id(db, "p1"))["status"] == "active"  # not stamped


@pytest.mark.asyncio
async def test_flood_cap_ignores_non_bulk_holds(db, monkeypatch):
    """Only BULK holds count toward the marketing flood cap — an unrelated
    IDENTITY/STANDARD hold does not block a marketing stage."""
    monkeypatch.setattr("genesis.outreach.marketing_config.max_pending_holds", lambda: 1)
    await pes.create(
        db,
        id="h_identity",
        request_id="r_identity",
        validated_recipient="someone@example.com",
        category="identity",
        message="a non-marketing reply",
        cell_domain="email",
        cell_verb="send",
        cell_risk_class="identity",
        held_at=_TS,
    )
    await mp.create(db, id="p1", email="a@example.com", created_at=_TS, updated_at=_TS)
    out = await _call("p1")
    assert out["status"] == "queued"  # the identity hold does not count


@pytest.mark.asyncio
async def test_flood_cap_counts_pending_outreach_queue(db, monkeypatch):
    """The cap must bound a single RUN: the campaign's subprocess path enqueues to
    pending_outreach (converted into HELD sends only by the */5min drain), so
    undelivered labeled_surplus rows count toward the cap too — otherwise the cap
    reads a stale post-drain table and never fires mid-run."""
    monkeypatch.setattr("genesis.outreach.marketing_config.max_pending_holds", lambda: 2)
    for i in range(2):
        await po.enqueue(
            db,
            message="queued pitch",
            category="notification",
            channel="email",
            validated_recipient=f"q{i}@example.com",
            labeled_surplus=True,
        )
    await mp.create(db, id="p1", email="a@example.com", created_at=_TS, updated_at=_TS)
    out = await _call("p1")
    assert out["status"] == "refused"
    assert out["reason"] == "approval_queue_full"


@pytest.mark.asyncio
async def test_refused_send_does_not_stamp_contacted(db):
    """Ordering guard: the stamp happens ONLY after a successful stage, so a
    refused send (opted-out) leaves the prospect status untouched."""
    await mp.create(db, id="p1", email="a@example.com", created_at=_TS, updated_at=_TS)
    await mp.mark_opted_out(db, "p1", opted_out_at=_TS)
    out = await _call("p1")
    assert out["status"] == "refused"
    assert (await mp.get_by_id(db, "p1"))["status"] == "active"  # never stamped
