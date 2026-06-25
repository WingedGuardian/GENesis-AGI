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
            outreach_id="o", status=self._status, channel="email", message_content="",
        )


class _FakeRt:
    def __init__(self, db, pipeline):
        self._db = db
        self._outreach_pipeline = pipeline


async def _hold(db, *, pid, rid):
    await pes.create(
        db, id=pid, request_id=rid, validated_recipient="a@b.c",
        category="outreach", message="body", held_at=_TS, **_CELL,
    )


async def _approval(db, *, status):
    mgr = ApprovalManager(db=db)
    rid = await mgr.request_approval(
        action_type="email_capability_gate", action_class="costly_reversible",
        description="d", context=json.dumps({"subject": "hi"}),
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
    assert (await ac.get_by_id(db, rid))["consumed_at"] is None  # not a real send
