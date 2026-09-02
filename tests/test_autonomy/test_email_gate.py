"""Tests for the WS-8 EmailAutonomyGate (the deterministic email gate).

Uses a real DB (full schema), a real ApprovalManager, and real capability /
pending CRUD — only the event bus is stubbed.  Covers: cold/ungranted → HOLD
(approval + pending rows, linked); granted-cell reply → ALLOW; FINANCIAL
hardline → HOLD without ever creating a financial cell; is_reply derivation
from email_thread_messages.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.autonomy.approval import ApprovalManager
from genesis.autonomy.email_gate import (
    _RATE_LIMIT_MAX,
    EMAIL_GATE_ACTION_TYPE,
    EmailAutonomyGate,
)
from genesis.autonomy.types import CellEvent
from genesis.db.crud import autonomous_email_sends as aes
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
        category=OutreachCategory.BLOCKER,
        topic="hi",
        context="body",
        salience_score=0.5,
        channel="email",
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


async def _add_inbound_from(db, thread_id, sender):
    """Inbound message with a KNOWN sender (for the recipient-match guard)."""
    await db.execute(
        "INSERT INTO email_thread_messages "
        "(thread_id, message_id, direction, sender, received_at) "
        "VALUES (?, ?, 'received', ?, ?)",
        (thread_id, f"m-{thread_id}-{sender}", sender, _TS),
    )
    await db.commit()


async def _grant_standard(db):
    for event in (CellEvent.CLASSIFY, CellEvent.APPROVE):
        await cg.apply_event(
            db,
            origin_class="first_party",
            domain="email",
            verb="send",
            risk_class="standard",
            event=event,
            updated_at=_TS,
        )


async def _grant_bulk(db):
    for event in (CellEvent.CLASSIFY, CellEvent.APPROVE):
        await cg.apply_event(
            db,
            origin_class="first_party",
            domain="email",
            verb="send",
            risk_class="bulk",
            event=event,
            updated_at=_TS,
        )


async def _add_prospect(db, *, email, opted_out=False, status="active"):
    from genesis.db.crud import marketing_prospects as mp

    pid = f"prospect-{email}"
    await mp.create(db, id=pid, email=email, status=status, created_at=_TS, updated_at=_TS)
    if opted_out:
        await mp.mark_opted_out(db, pid, opted_out_at=_TS)


@pytest.fixture
def marketing_live(monkeypatch):
    """Force the marketing lever to ``live`` — the posture under which a GRANTED
    BULK cell may autonomously send. The gate reads ``effective_mode()`` via a
    local import, so patching the module attribute takes effect at call time."""
    monkeypatch.setattr("genesis.outreach.marketing_config.effective_mode", lambda: "live")


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
    # pre-grant the standard (known-thread reply) cell, with the recipient as a
    # recorded thread participant so the g1 recipient-match guard passes.
    await cg.apply_event(
        db,
        origin_class="first_party",
        domain="email",
        verb="send",
        risk_class="standard",
        event=CellEvent.CLASSIFY,
        updated_at=_TS,
    )
    await cg.apply_event(
        db,
        origin_class="first_party",
        domain="email",
        verb="send",
        risk_class="standard",
        event=CellEvent.APPROVE,
        updated_at=_TS,
    )
    await _add_inbound_from(db, "t1", "alice@example.com")

    gate = _gate(db)
    req = _req(validated_recipient="alice@example.com", thread_id="t1")
    decision = await gate.check(request=req, recipient="alice@example.com", message_text="re: hi")

    assert decision.allow is True
    assert decision.reason == "granted"
    assert await pes.list_held(db) == []  # nothing held


@pytest.mark.asyncio
async def test_financial_is_hardline_held_without_a_cell(db):
    # Even pre-granting a financial cell must not let a financial email through.
    await cg.apply_event(
        db,
        origin_class="first_party",
        domain="email",
        verb="send",
        risk_class="financial",
        event=CellEvent.CLASSIFY,
        updated_at=_TS,
    )
    await cg.apply_event(
        db,
        origin_class="first_party",
        domain="email",
        verb="send",
        risk_class="financial",
        event=CellEvent.APPROVE,
        updated_at=_TS,
    )
    gate = _gate(db)
    req = _req(validated_recipient="alice@example.com", thread_id="t1")
    decision = await gate.check(
        request=req,
        recipient="alice@example.com",
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
        action_type=EMAIL_GATE_ACTION_TYPE,
        action_class="costly_reversible",
        description="email send",
    )
    other_rid = await mgr.request_approval(
        action_type="autonomous_cli_fallback",
        action_class="reversible",
        description="cli action",
    )
    gate = AutonomousCliApprovalGate(runtime=MagicMock(), approval_manager=mgr)

    n = await gate.approve_all_pending(resolved_by="user")
    assert n == 1  # only the non-email approval
    assert (await ac.get_by_id(db, email_rid))["status"] == "pending"  # still held
    assert (await ac.get_by_id(db, other_rid))["status"] == "approved"


# --------------------------------------------------------------------------- #
# WS-8 PR-D — deterministic pre-send scope guards on a GRANTED cell
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_granted_reply_recipient_mismatch_demotes_and_holds(db):
    # g1: the granted standard cell's recipient is NOT a participant of the thread
    # → scope drift → hold THIS send + demote the cell GRANTED→ASK.
    await _grant_standard(db)
    await _add_inbound_from(db, "t1", "alice@example.com")  # known correspondent
    gate = _gate(db)
    req = _req(validated_recipient="mallory@evil.com", thread_id="t1")
    decision = await gate.check(
        request=req,
        recipient="mallory@evil.com",
        message_text="re",
    )
    assert decision.allow is False  # held
    cell = await cg.get_cell(db, "email", "send", "standard")
    assert cell["state"] == "ask"  # demoted
    assert cell["corrections"] == 1


@pytest.mark.asyncio
async def test_granted_reply_unknown_sender_trips_guard(db):
    # A thread whose only inbound has no recorded sender is genuinely-ambiguous
    # scope — g1 trips (hold + demote) rather than waving any recipient through.
    # The real reply path always records a sender (reply_poller/record_reply), so
    # this only affects anomalous/unparsed rows; the SAFE failure is to hold.
    await _grant_standard(db)
    await _add_inbound(db, "t1")  # NULL sender (anomalous)
    gate = _gate(db)
    req = _req(validated_recipient="anyone@example.com", thread_id="t1")
    decision = await gate.check(
        request=req,
        recipient="anyone@example.com",
        message_text="re",
    )
    assert decision.allow is False  # held — ambiguous scope is not waved through
    assert (await cg.get_cell(db, "email", "send", "standard"))["state"] == "ask"


# --------------------------------------------------------------------------- #
# Cold marketing (BULK) — labeled_surplus path + the marketing scope guard
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_labeled_surplus_classifies_bulk_cell_key(db):
    # (d) labeled_surplus=True → the classifier cell key is exactly
    # ("email","send","bulk").  Cold + ungranted → HELD on the bulk cell.
    gate = _gate(db)
    req = _req(validated_recipient="prospect@example.com", thread_id=None, labeled_surplus=True)
    decision = await gate.check(
        request=req,
        recipient="prospect@example.com",
        message_text="cold pitch",
    )
    assert decision.allow is False  # bulk cell ungranted (ASK) → held
    pend = await pes.get_by_request(db, decision.request_id)
    assert (pend["cell_domain"], pend["cell_verb"], pend["cell_risk_class"]) == (
        "email",
        "send",
        "bulk",
    )


@pytest.mark.asyncio
async def test_bulk_cold_ungranted_is_held(db):
    # (a) a bulk send with a validated_recipient + no inbound → HELD; a
    # pending_email_sends row is written (adapter is never reached at the gate).
    gate = _gate(db)
    req = _req(validated_recipient="prospect@example.com", thread_id=None, labeled_surplus=True)
    decision = await gate.check(
        request=req,
        recipient="prospect@example.com",
        message_text="hi",
    )
    assert decision.allow is False
    assert await pes.get_by_request(db, decision.request_id) is not None


@pytest.mark.asyncio
async def test_granted_bulk_unknown_recipient_demotes_and_holds(db):
    # NEW guard: a GRANTED bulk cell whose recipient is NOT a curated prospect
    # → scope drift → hold THIS send + demote GRANTED→ASK.
    await _grant_bulk(db)
    await _add_prospect(db, email="known@example.com")  # active
    gate = _gate(db)
    req = _req(validated_recipient="stranger@example.com", thread_id=None, labeled_surplus=True)
    decision = await gate.check(
        request=req,
        recipient="stranger@example.com",
        message_text="hi",
    )
    assert decision.allow is False
    cell = await cg.get_cell(db, "email", "send", "bulk")
    assert cell["state"] == "ask"  # demoted
    assert cell["corrections"] == 1


@pytest.mark.asyncio
async def test_granted_bulk_opted_out_recipient_demotes_and_holds(db):
    # A curated-but-opted-out prospect is NOT in the active set → trip + demote
    # (fresh cell so the demote-then-regrant transition never runs).
    await _grant_bulk(db)
    await _add_prospect(db, email="gone@example.com", opted_out=True)
    gate = _gate(db)
    req = _req(validated_recipient="gone@example.com", thread_id=None, labeled_surplus=True)
    decision = await gate.check(
        request=req,
        recipient="gone@example.com",
        message_text="hi",
    )
    assert decision.allow is False
    assert (await cg.get_cell(db, "email", "send", "bulk"))["state"] == "ask"


@pytest.mark.asyncio
async def test_granted_bulk_recipient_in_active_set_allowed(db, marketing_live):
    # A GRANTED bulk cell whose recipient IS an active prospect + under the rate
    # limit → the guard falls through to allow (autonomous send). Requires the
    # marketing lever at `live` — the affirmative autonomous-cold-outreach posture.
    await _grant_bulk(db)
    await _add_prospect(db, email="known@example.com")
    gate = _gate(db)
    req = _req(validated_recipient="known@example.com", thread_id=None, labeled_surplus=True)
    decision = await gate.check(
        request=req,
        recipient="known@example.com",
        message_text="hi",
    )
    assert decision.allow is True
    assert decision.reason == "granted"
    assert await pes.list_held(db) == []


@pytest.mark.asyncio
async def test_granted_bulk_contacted_recipient_still_allowed(db, marketing_live):
    # Authorization is CURATED + NOT opted-out, DECOUPLED from send-lifecycle status.
    # A prospect already stamped 'contacted' (a legitimate follow-up touch) is STILL
    # authorized — the guard must NOT trip on non-'active' status, which would demote
    # the GRANTED cell the instant a prior send stamped the prospect contacted.
    # (RED on the pre-fix guard, which used an active-status membership predicate.)
    await _grant_bulk(db)
    await _add_prospect(db, email="repeat@example.com", status="contacted")
    gate = _gate(db)
    req = _req(validated_recipient="repeat@example.com", thread_id=None, labeled_surplus=True)
    decision = await gate.check(
        request=req,
        recipient="repeat@example.com",
        message_text="hi",
    )
    assert decision.allow is True
    assert decision.reason == "granted"
    assert (await cg.get_cell(db, "email", "send", "bulk"))["state"] == "granted"  # NOT demoted
    assert await pes.list_held(db) == []


@pytest.mark.asyncio
async def test_granted_bulk_recipient_case_insensitive(db, marketing_live):
    # Recipient case/whitespace must not defeat authorization OR opt-out suppression:
    # the store normalizes to lowercase, so a mixed-case send resolves the same row.
    await _grant_bulk(db)
    await _add_prospect(db, email="Known@Example.com")  # stored lowercased
    gate = _gate(db)
    req = _req(validated_recipient="KNOWN@example.com", thread_id=None, labeled_surplus=True)
    decision = await gate.check(
        request=req,
        recipient="KNOWN@example.com",
        message_text="hi",
    )
    assert decision.allow is True
    assert decision.reason == "granted"


@pytest.mark.parametrize("mode", ["observe", "off"])
@pytest.mark.asyncio
async def test_granted_bulk_non_live_mode_holds_without_demote(db, monkeypatch, mode):
    # Codex round-2 P1: a GRANTED BULK cell must NOT autonomously send while the
    # marketing lever is `observe` (or `off`) — only `live` affirmatively enables
    # autonomous cold outreach (marketing_config: observe = lower authority). The
    # send HOLDS for owner approval, and critically the cell is NOT demoted
    # (a non-live posture is a legitimate owner choice, not scope drift) — so
    # flipping to `live` later resumes autonomy without a re-grant.
    monkeypatch.setattr("genesis.outreach.marketing_config.effective_mode", lambda: mode)
    await _grant_bulk(db)
    await _add_prospect(db, email="known@example.com")  # active, curated
    gate = _gate(db)
    req = _req(validated_recipient="known@example.com", thread_id=None, labeled_surplus=True)
    decision = await gate.check(
        request=req,
        recipient="known@example.com",
        message_text="hi",
    )
    assert decision.allow is False  # held, not autonomously sent
    assert await pes.get_by_request(db, decision.request_id) is not None  # held row written
    # NOT demoted: observe is a posture, not a scope violation.
    assert (await cg.get_cell(db, "email", "send", "bulk"))["state"] == "granted"


@pytest.mark.asyncio
async def test_granted_rate_limit_burst_demotes_and_holds(db):
    # g3 (primary): a burst of autonomous sends for one cell within the window =
    # a runaway loop → hold + demote.  g1 passes (recipient matches the thread).
    await _grant_standard(db)
    await _add_inbound_from(db, "t1", "alice@example.com")
    now = datetime.now(UTC).isoformat()
    for i in range(_RATE_LIMIT_MAX):
        await aes.create(
            db,
            id=f"a{i}",
            recipient="alice@example.com",
            sent_at=now,
            cell_domain="email",
            cell_verb="send",
            cell_risk_class="standard",
        )
    gate = _gate(db)
    req = _req(validated_recipient="alice@example.com", thread_id="t1")
    decision = await gate.check(
        request=req,
        recipient="alice@example.com",
        message_text="re",
    )
    assert decision.allow is False  # rate-limit tripped
    assert (await cg.get_cell(db, "email", "send", "standard"))["state"] == "ask"
