"""Tests for the WS-8 email gate resolution watcher (drain_pending_email_sends).

Real DB + real ApprovalManager + a fake pipeline (so we control delivery
outcome). Covers: approved → sent + consumed + record_success; rejected →
record_correction (never sent); orphaned → expired; pending → left held;
approved-but-delivery-fails → left held, approval NOT consumed (retried).
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from genesis.autonomy import email_gate_watcher as egw
from genesis.autonomy.approval import ApprovalManager
from genesis.autonomy.email_gate_watcher import drain_pending_email_sends
from genesis.db.crud import approval_requests as ac
from genesis.db.crud import capability_grants as cg
from genesis.db.crud import pending_email_sends as pes
from genesis.db.schema import create_all_tables
from genesis.outreach.types import OutreachResult, OutreachStatus

_TS = "2026-06-21T00:00:00"
_CELL = {"cell_domain": "email", "cell_verb": "send", "cell_risk_class": "standard"}


@pytest.fixture
async def db(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    await conn.commit()
    yield conn
    await conn.close()


class _FakePipeline:
    def __init__(self, status):
        self._status = status
        self.calls: list = []

    async def deliver_approved(self, row, *, subject=None):
        self.calls.append((row["id"], subject))
        return OutreachResult(
            outreach_id="o",
            status=self._status,
            channel="email",
            message_content="",
        )


class _FakeRt:
    def __init__(self, db, pipeline):
        self._db = db
        self._outreach_pipeline = pipeline


async def _hold(db, *, pid, rid):
    await pes.create(
        db,
        id=pid,
        request_id=rid,
        validated_recipient="a@b.c",
        category="outreach",
        message="body",
        held_at=_TS,
        **_CELL,
    )


async def _approval(db, *, status):
    mgr = ApprovalManager(db=db)
    rid = await mgr.request_approval(
        action_type="email_capability_gate",
        action_class="costly_reversible",
        description="d",
        context=json.dumps({"subject": "hi"}),
    )
    if status != "pending":
        await mgr.resolve(rid, status=status)
    return rid


@pytest.mark.asyncio
async def test_approved_hold_is_sent(db):
    rid = await _approval(db, status="approved")
    await _hold(db, pid="p1", rid=rid)
    pipe = _FakePipeline(OutreachStatus.DELIVERED)

    assert await drain_pending_email_sends(_FakeRt(db, pipe)) == 1
    assert pipe.calls == [("p1", "hi")]  # delivered verbatim, with subject
    assert (await pes.get_by_id(db, "p1"))["status"] == "sent"
    assert (await ac.get_by_id(db, rid))["consumed_at"] is not None
    assert (await cg.get_cell(db, "email", "send", "standard"))["successes"] == 1


@pytest.mark.asyncio
async def test_approved_bulk_hold_sends_and_increments_owner_success(db, monkeypatch):
    monkeypatch.setattr(egw, "_marketing_mode", lambda: "live")  # armed — kill-switch not active
    # (e) approve→drain for a BULK (cold marketing) hold: deliver_approved sends
    # once and the BULK cell's successes increments with origin_class="owner".
    # The recipient must still be a curated, non-opted-out prospect at send time
    # (the approved-send-time opt-out re-check reuses g2's predicate).
    from genesis.db.crud import marketing_prospects as mp

    await mp.create(db, id="prospect", email="prospect@example.com", created_at=_TS, updated_at=_TS)
    rid = await _approval(db, status="approved")
    await pes.create(
        db,
        id="pb",
        request_id=rid,
        validated_recipient="prospect@example.com",
        category="notification",
        message="cold pitch",
        held_at=_TS,
        cell_domain="email",
        cell_verb="send",
        cell_risk_class="bulk",
    )
    pipe = _FakePipeline(OutreachStatus.DELIVERED)

    assert await drain_pending_email_sends(_FakeRt(db, pipe)) == 1
    assert pipe.calls == [("pb", "hi")]  # sent once
    assert (await pes.get_by_id(db, "pb"))["status"] == "sent"
    # The watcher records the success with origin_class="owner" (cell-agnostic,
    # hardcoded) — for a BULK cell the counter must increment just like standard.
    cell = await cg.get_cell(db, "email", "send", "bulk")
    assert cell["successes"] == 1


@pytest.mark.asyncio
async def test_approved_bulk_hold_kill_switch_off_not_delivered(db):
    # F9 (Codex): the marketing kill-switch (lever `off` / env-kill) must halt an
    # already-approved BULK cold-marketing hold at DELIVERY too — not just at enqueue.
    # With effective_mode()=='off' (the shipped default here) the approved BULK hold is
    # PAUSED: left held, approval NOT consumed, nothing sent — so a re-enable resumes it
    # and the owner's outer off-switch is honored deliver-side. Non-marketing behavior
    # is unchanged (only cell_risk_class=='bulk' is gated on the marketing lever).
    from genesis.db.crud import marketing_prospects as mp

    await mp.create(db, id="prospect", email="prospect@example.com", created_at=_TS, updated_at=_TS)
    rid = await _approval(db, status="approved")
    await pes.create(
        db,
        id="pb",
        request_id=rid,
        validated_recipient="prospect@example.com",
        category="notification",
        message="cold pitch",
        held_at=_TS,
        cell_domain="email",
        cell_verb="send",
        cell_risk_class="bulk",
    )
    pipe = _FakePipeline(OutreachStatus.DELIVERED)
    # effective_mode() defaults to 'off' (shipped config, no overlay) → kill-switch active.
    assert await drain_pending_email_sends(_FakeRt(db, pipe)) == 0
    assert pipe.calls == []  # NOT delivered — kill-switch honored at delivery
    assert (await pes.get_by_id(db, "pb"))["status"] == "held"  # paused, not consumed
    assert (await ac.get_by_id(db, rid))["consumed_at"] is None


@pytest.mark.asyncio
async def test_approved_bulk_hold_opted_out_after_hold_not_delivered(db, monkeypatch):
    monkeypatch.setattr(egw, "_marketing_mode", lambda: "live")  # armed — isolate the opt-out path
    # Opt-out re-check at approved-send time: a prospect who opts out AFTER the
    # bulk hold is enqueued but BEFORE the owner approves must NOT be delivered on
    # the gate_cleared resume path (the g2 scope guard is bypassed there). The
    # watcher re-applies the SAME predicate g2 uses immediately before delivery.
    from genesis.db.crud import marketing_prospects as mp

    await mp.create(db, id="prospect", email="prospect@example.com", created_at=_TS, updated_at=_TS)
    await mp.mark_opted_out(db, "prospect", opted_out_at=_TS)  # opts out AFTER hold
    rid = await _approval(db, status="approved")
    await pes.create(
        db,
        id="pb",
        request_id=rid,
        validated_recipient="prospect@example.com",
        category="notification",
        message="cold pitch",
        held_at=_TS,
        cell_domain="email",
        cell_verb="send",
        cell_risk_class="bulk",
    )
    pipe = _FakePipeline(OutreachStatus.DELIVERED)

    assert await drain_pending_email_sends(_FakeRt(db, pipe)) == 1
    assert pipe.calls == []  # NOT delivered — opt-out re-check blocked it
    assert (await pes.get_by_id(db, "pb"))["status"] == "rejected"  # terminal
    # No delivery ⇒ no owner success recorded for the BULK cell.
    assert await cg.get_cell(db, "email", "send", "bulk") is None


@pytest.mark.asyncio
async def test_approved_bulk_hold_not_curated_after_hold_not_delivered(db, monkeypatch):
    monkeypatch.setattr(egw, "_marketing_mode", lambda: "live")  # armed — isolate the curation path
    # Companion: recipient removed from (or never present in) the curated set at
    # approve time → not authorized → not delivered, marked rejected.
    rid = await _approval(db, status="approved")
    await pes.create(
        db,
        id="pb",
        request_id=rid,
        validated_recipient="ghost@example.com",
        category="notification",
        message="cold pitch",
        held_at=_TS,
        cell_domain="email",
        cell_verb="send",
        cell_risk_class="bulk",
    )
    pipe = _FakePipeline(OutreachStatus.DELIVERED)

    assert await drain_pending_email_sends(_FakeRt(db, pipe)) == 1
    assert pipe.calls == []
    assert (await pes.get_by_id(db, "pb"))["status"] == "rejected"


@pytest.mark.asyncio
async def test_approved_financial_hold_opted_out_prospect_not_delivered(db):
    # ABSOLUTE opt-out re-check (any class). A cold marketing pitch whose body trips
    # the FINANCIAL money-pattern classifier is stored cell_risk_class="financial"
    # (NOT "bulk"), so the bulk-gated re-check would skip it — but an opted-out
    # prospect must never receive an autonomous send. The absolute _recipient_opted_out
    # guard refuses it regardless of class.
    from genesis.db.crud import marketing_prospects as mp

    await mp.create(db, id="prospect", email="prospect@example.com", created_at=_TS, updated_at=_TS)
    await mp.mark_opted_out(db, "prospect", opted_out_at=_TS)
    rid = await _approval(db, status="approved")
    await pes.create(
        db,
        id="pf",
        request_id=rid,
        validated_recipient="prospect@example.com",
        category="notification",
        message="invoice attached — please remit payment",  # money pattern → FINANCIAL
        held_at=_TS,
        cell_domain="email",
        cell_verb="send",
        cell_risk_class="financial",  # NOT bulk — the gap the absolute check closes
    )
    pipe = _FakePipeline(OutreachStatus.DELIVERED)

    assert await drain_pending_email_sends(_FakeRt(db, pipe)) == 1
    assert pipe.calls == []  # refused despite non-bulk class
    assert (await pes.get_by_id(db, "pf"))["status"] == "rejected"  # terminal
    assert await cg.get_cell(db, "email", "send", "financial") is None  # no success


@pytest.mark.asyncio
async def test_approved_financial_hold_non_prospect_still_delivered(db):
    # Residue boundary: the absolute opt-out check is NARROW — it only trips on a
    # KNOWN opted-out prospect. A legitimate FINANCIAL email to someone who is NOT
    # in the prospect store is delivered normally (the helper returns False on a
    # missing row, so a non-marketing send is never collateral-blocked).
    rid = await _approval(db, status="approved")
    await pes.create(
        db,
        id="pf",
        request_id=rid,
        validated_recipient="vendor@example.com",  # not a prospect
        category="notification",
        message="invoice attached — please remit payment",
        held_at=_TS,
        cell_domain="email",
        cell_verb="send",
        cell_risk_class="financial",
    )
    pipe = _FakePipeline(OutreachStatus.DELIVERED)

    assert await drain_pending_email_sends(_FakeRt(db, pipe)) == 1
    assert pipe.calls == [("pf", "hi")]  # delivered — not blocked
    assert (await pes.get_by_id(db, "pf"))["status"] == "sent"


@pytest.mark.asyncio
async def test_rejected_hold_records_correction(db):
    rid = await _approval(db, status="rejected")
    await _hold(db, pid="p1", rid=rid)
    pipe = _FakePipeline(OutreachStatus.DELIVERED)

    assert await drain_pending_email_sends(_FakeRt(db, pipe)) == 1
    assert pipe.calls == []  # never sent
    assert (await pes.get_by_id(db, "p1"))["status"] == "rejected"
    assert (await cg.get_cell(db, "email", "send", "standard"))["corrections"] == 1


@pytest.mark.asyncio
async def test_orphaned_hold_expired(db):
    await _hold(db, pid="p1", rid="no-such-approval")
    pipe = _FakePipeline(OutreachStatus.DELIVERED)

    assert await drain_pending_email_sends(_FakeRt(db, pipe)) == 1
    assert pipe.calls == []
    assert (await pes.get_by_id(db, "p1"))["status"] == "expired"


@pytest.mark.asyncio
async def test_pending_approval_left_held(db):
    rid = await _approval(db, status="pending")
    await _hold(db, pid="p1", rid=rid)
    pipe = _FakePipeline(OutreachStatus.DELIVERED)

    assert await drain_pending_email_sends(_FakeRt(db, pipe)) == 0
    assert pipe.calls == []
    assert (await pes.get_by_id(db, "p1"))["status"] == "held"


@pytest.mark.asyncio
async def test_already_consumed_approval_is_reconciled_not_resent(db):
    # Approval delivered + consumed by a prior cycle that crashed before marking
    # the hold sent → reconcile WITHOUT re-sending (double-send guard).
    rid = await _approval(db, status="approved")
    await ac.mark_consumed(db, rid, consumed_at=_TS)
    await _hold(db, pid="p1", rid=rid)
    pipe = _FakePipeline(OutreachStatus.DELIVERED)

    assert await drain_pending_email_sends(_FakeRt(db, pipe)) == 1
    assert pipe.calls == []  # NOT re-sent
    assert (await pes.get_by_id(db, "p1"))["status"] == "sent"


@pytest.mark.asyncio
async def test_approved_but_delivery_fails_stays_held(db):
    rid = await _approval(db, status="approved")
    await _hold(db, pid="p1", rid=rid)
    pipe = _FakePipeline(OutreachStatus.FAILED)

    assert await drain_pending_email_sends(_FakeRt(db, pipe)) == 0
    assert pipe.calls == [("p1", "hi")]  # attempted
    assert (await pes.get_by_id(db, "p1"))["status"] == "held"  # retry next cycle
    assert (await ac.get_by_id(db, rid))["consumed_at"] is None  # not consumed


@pytest.mark.asyncio
async def test_approved_but_pipeline_ignores_is_terminal(db):
    """If the pipeline terminally skips an approved send (IGNORED — e.g. a
    self-addressed hold the new guard drops), the watcher must mark it rejected,
    NOT leave it held to busy-loop every cycle."""
    rid = await _approval(db, status="approved")
    await _hold(db, pid="p1", rid=rid)
    pipe = _FakePipeline(OutreachStatus.IGNORED)

    assert await drain_pending_email_sends(_FakeRt(db, pipe)) == 1
    assert pipe.calls == [("p1", "hi")]  # attempted (deliver_approved called)
    assert (await pes.get_by_id(db, "p1"))["status"] == "rejected"  # terminal
    # The approval lifecycle is closed (consumed) so it doesn't linger as a
    # ghost approved/unconsumed row in operator views.
    assert (await ac.get_by_id(db, rid))["consumed_at"] is not None


# --- loop-fix: prospect contact-stamping on CONFIRMED delivery -----------------


async def _bulk_hold_for(db, *, prospect_email, rid, cell_risk_class="bulk", pid="pb"):
    await pes.create(
        db,
        id=pid,
        request_id=rid,
        validated_recipient=prospect_email,
        category="notification",
        message="cold pitch",
        held_at=_TS,
        cell_domain="email",
        cell_verb="send",
        cell_risk_class=cell_risk_class,
    )


@pytest.mark.asyncio
async def test_delivered_bulk_hold_stamps_prospect_contacted(db, monkeypatch):
    """Confirmed delivery of a cold-marketing (BULK) send advances the prospect
    active → contacted at the DRAIN, so list_active stops re-pitching it — the
    loop-fix (mark_contacted previously had no caller)."""
    monkeypatch.setattr(egw, "_marketing_mode", lambda: "live")
    from genesis.db.crud import marketing_prospects as mp

    await mp.create(db, id="prospect", email="prospect@example.com", created_at=_TS, updated_at=_TS)
    rid = await _approval(db, status="approved")
    await _bulk_hold_for(db, prospect_email="prospect@example.com", rid=rid)

    assert (
        await drain_pending_email_sends(_FakeRt(db, _FakePipeline(OutreachStatus.DELIVERED))) == 1
    )
    row = await mp.get_by_id(db, "prospect")
    assert row["status"] == "contacted"
    assert row["last_contacted_at"] is not None
    assert await mp.list_active(db) == []


@pytest.mark.asyncio
async def test_delivered_financial_misclassified_pitch_still_stamps(db):
    """Provenance is RECIPIENT-based, NOT cell_risk_class: a cold pitch whose body
    tripped the money-pattern classifier holds as FINANCIAL (not BULK), but on
    delivery to a curated prospect it must STILL stamp them — else re-pitched."""
    from genesis.db.crud import marketing_prospects as mp

    await mp.create(db, id="prospect", email="prospect@example.com", created_at=_TS, updated_at=_TS)
    rid = await _approval(db, status="approved")
    await _bulk_hold_for(
        db,
        prospect_email="prospect@example.com",
        rid=rid,
        cell_risk_class="financial",
        pid="pf",
    )

    assert (
        await drain_pending_email_sends(_FakeRt(db, _FakePipeline(OutreachStatus.DELIVERED))) == 1
    )
    assert (await mp.get_by_id(db, "prospect"))["status"] == "contacted"


@pytest.mark.asyncio
async def test_non_delivery_outcome_leaves_prospect_active(db):
    """Anti-burn: the stamp fires ONLY on confirmed delivery. An orphaned hold
    (approval gone → expired, no delivery) leaves the prospect 'active', re-eligible
    — a send that never delivers never closes a prospect. Owner-rejection likewise
    leaves it active (re-eligible for a re-worded pitch; opt-out is the 'never')."""
    from genesis.db.crud import marketing_prospects as mp

    await mp.create(db, id="prospect", email="prospect@example.com", created_at=_TS, updated_at=_TS)
    await _bulk_hold_for(db, prospect_email="prospect@example.com", rid="gone")  # no approval

    assert (
        await drain_pending_email_sends(_FakeRt(db, _FakePipeline(OutreachStatus.DELIVERED))) == 1
    )
    assert (await mp.get_by_id(db, "prospect"))["status"] == "active"  # not burned
    assert len(await mp.list_active(db)) == 1


@pytest.mark.asyncio
async def test_reconciled_consumed_hold_stamps_prospect(db):
    """Crash-recovery: a hold whose approval was ALREADY consumed (delivered by a
    prior cycle that crashed before marking 'sent') reconciles to sent AND stamps the
    prospect — a crash between delivery and the stamp must not leave a delivered
    prospect 'active' and re-pitchable. Not re-sent (pipe.calls stays empty)."""
    from genesis.db.crud import marketing_prospects as mp

    await mp.create(db, id="prospect", email="prospect@example.com", created_at=_TS, updated_at=_TS)
    rid = await _approval(db, status="approved")
    await ac.mark_consumed(db, rid, consumed_at=_TS)
    await _bulk_hold_for(db, prospect_email="prospect@example.com", rid=rid)

    pipe = _FakePipeline(OutreachStatus.DELIVERED)
    assert await drain_pending_email_sends(_FakeRt(db, pipe)) == 1
    assert pipe.calls == []  # NOT re-sent
    assert (await mp.get_by_id(db, "prospect"))["status"] == "contacted"


@pytest.mark.asyncio
async def test_reconciled_hold_stamps_prospect_before_terminalizing(db, monkeypatch):
    """Crash-safety of the reconcile branch: the prospect stamp must be durable
    BEFORE the hold goes terminal (mark_sent). If the terminal write fails (DB
    error) — or the process stops right after mark_sent commits — the hold leaves
    list_held forever while the prospect is still 'active' → re-pitched. Simulate
    the terminal write raising at exactly that point and assert the prospect was
    ALREADY advanced to 'contacted' (i.e. stamp precedes mark_sent, matching the
    DELIVERED branch). RED on the old mark_sent-first order."""
    from genesis.db.crud import marketing_prospects as mp

    await mp.create(db, id="prospect", email="prospect@example.com", created_at=_TS, updated_at=_TS)
    rid = await _approval(db, status="approved")
    await ac.mark_consumed(db, rid, consumed_at=_TS)
    await _bulk_hold_for(db, prospect_email="prospect@example.com", rid=rid)

    async def _boom(*a, **k):
        raise aiosqlite.OperationalError("simulated crash terminalizing the hold")

    monkeypatch.setattr(pes, "mark_sent", _boom)

    pipe = _FakePipeline(OutreachStatus.DELIVERED)
    with pytest.raises(aiosqlite.OperationalError):
        await drain_pending_email_sends(_FakeRt(db, pipe))

    assert pipe.calls == []  # reconcile never re-sends
    # The stamp must have committed before the failed terminal write.
    assert (await mp.get_by_id(db, "prospect"))["status"] == "contacted"
