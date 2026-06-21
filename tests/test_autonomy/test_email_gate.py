"""Tests for the WS-8 EmailAutonomyGate (the deterministic email gate).

Uses a real DB (full schema), a real ApprovalManager, and real capability /
pending CRUD — only the event bus is stubbed.  Covers: cold/ungranted → HOLD
(approval + pending rows, linked); granted-cell reply → ALLOW; FINANCIAL
hardline → HOLD without ever creating a financial cell; is_reply derivation
from email_thread_messages.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.autonomy.approval import ApprovalManager
from genesis.autonomy.email_gate import EMAIL_GATE_ACTION_TYPE, EmailAutonomyGate
from genesis.autonomy.types import CellEvent
from genesis.db.crud import capability_grants as cg
from genesis.db.crud import pending_email_sends as pes
from genesis.db.schema import create_all_tables
from genesis.outreach.types import OutreachCategory, OutreachRequest

_TS = "2026-06-21T00:00:00"


@pytest.fixture
async def db(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    await conn.commit()
    yield conn
    await conn.close()


def _gate(db):
    return EmailAutonomyGate(db=db, approval_manager=ApprovalManager(db=db), event_bus=None)


def _req(**kw):
    base = dict(
        category=OutreachCategory.BLOCKER, topic="hi", context="body",
        salience_score=0.5, channel="email",
    )
    base.update(kw)
    return OutreachRequest(**base)


async def _add_inbound(db, thread_id):
    """Insert one received message so is_reply derives True (the gate's
    _has_inbound only reads email_thread_messages)."""
    await db.execute(
        "INSERT INTO email_thread_messages "
        "(thread_id, message_id, direction, received_at) VALUES (?, ?, 'received', ?)",
        (thread_id, f"m-{thread_id}", _TS),
    )
    await db.commit()


# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cold_ungranted_email_is_held(db):
    gate = _gate(db)
    req = _req(validated_recipient=None, thread_id=None)  # cold, recipient unknown
    decision = await gate.check(request=req, recipient="bob@example.com", message_text="hello")

    assert decision.allow is False
    assert decision.pending_id and decision.request_id
    pend = await pes.get_by_request(db, decision.request_id)
    assert pend["status"] == "held"
    assert pend["validated_recipient"] == "bob@example.com"
    assert pend["cell_risk_class"] == "identity"  # cold → identity
    # linked approval row of the isolated action_type
    cur = await db.execute(
        "SELECT action_type, status FROM approval_requests WHERE id = ?",
        (decision.request_id,),
    )
    row = await cur.fetchone()
    assert row["action_type"] == EMAIL_GATE_ACTION_TYPE and row["status"] == "pending"


@pytest.mark.asyncio
async def test_granted_known_thread_reply_is_allowed(db):
    # pre-grant the standard (known-thread reply) cell — the Option-B seed.
    await cg.apply_event(db, domain="email", verb="send", risk_class="standard",
                         event=CellEvent.CLASSIFY, updated_at=_TS)
    await cg.apply_event(db, domain="email", verb="send", risk_class="standard",
                         event=CellEvent.APPROVE, updated_at=_TS)
    await _add_inbound(db, "t1")

    gate = _gate(db)
    req = _req(validated_recipient="alice@example.com", thread_id="t1")
    decision = await gate.check(request=req, recipient="alice@example.com", message_text="re: hi")

    assert decision.allow is True
    assert decision.reason == "granted"
    assert await pes.list_held(db) == []  # nothing held


@pytest.mark.asyncio
async def test_financial_is_hardline_held_without_a_cell(db):
    # Even pre-granting a financial cell must not let a financial email through.
    await cg.apply_event(db, domain="email", verb="send", risk_class="financial",
                         event=CellEvent.CLASSIFY, updated_at=_TS)
    await cg.apply_event(db, domain="email", verb="send", risk_class="financial",
                         event=CellEvent.APPROVE, updated_at=_TS)
    gate = _gate(db)
    req = _req(validated_recipient="alice@example.com", thread_id="t1")
    decision = await gate.check(
        request=req, recipient="alice@example.com",
        message_text="Please wire transfer the invoice balance to this IBAN.",
    )
    assert decision.allow is False  # hardline — held despite the granted cell
    pend = await pes.get_by_request(db, decision.request_id)
    assert pend["cell_risk_class"] == "financial"


@pytest.mark.asyncio
async def test_ungranted_reply_classifies_standard(db):
    await _add_inbound(db, "t2")
    gate = _gate(db)
    req = _req(validated_recipient="alice@example.com", thread_id="t2")
    decision = await gate.check(request=req, recipient="alice@example.com", message_text="re")
    # standard cell isn't granted (no seed here) → held, but classified standard.
    assert decision.allow is False
    pend = await pes.get_by_request(db, decision.request_id)
    assert pend["cell_risk_class"] == "standard"


@pytest.mark.asyncio
async def test_approve_all_pending_excludes_email_gate(db):
    """Batch 'approve all' must never sweep email-gate holds (each is its own
    send decision)."""
    from unittest.mock import MagicMock

    from genesis.autonomy.approval_gate import AutonomousCliApprovalGate
    from genesis.db.crud import approval_requests as ac

    mgr = ApprovalManager(db=db)
    email_rid = await mgr.request_approval(
        action_type=EMAIL_GATE_ACTION_TYPE, action_class="costly_reversible",
        description="email send",
    )
    other_rid = await mgr.request_approval(
        action_type="autonomous_cli_fallback", action_class="reversible",
        description="cli action",
    )
    gate = AutonomousCliApprovalGate(runtime=MagicMock(), approval_manager=mgr)

    n = await gate.approve_all_pending(resolved_by="user")
    assert n == 1  # only the non-email approval
    assert (await ac.get_by_id(db, email_rid))["status"] == "pending"  # still held
    assert (await ac.get_by_id(db, other_rid))["status"] == "approved"
