"""WS-8 PR-D full-slice E2E — the headline earn/lose loop through REAL components
(gate + pipeline + resolution watcher + promotion handler + flag), only the email
adapter is faked.

Narrative asserted end to end:
  cold reply HOLDS (all-ASK) → owner approves N → cell earns competence →
  becomes promotable → owner approves a promotion proposal → cell GRANTED →
  a reply now sends AUTONOMOUSLY and is logged → owner flags it as bad →
  the cell craters back to ASK (trust is easy to lose).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.autonomy.approval import ApprovalManager
from genesis.autonomy.email_gate import EmailAutonomyGate
from genesis.autonomy.email_gate_watcher import drain_pending_email_sends
from genesis.autonomy.types import CellState
from genesis.content.types import FormatTarget, FormattedContent
from genesis.db.crud import autonomous_email_sends as aes
from genesis.db.crud import capability_grants as cg
from genesis.db.crud import pending_email_sends as pes
from genesis.db.schema import create_all_tables
from genesis.ego.cadence import EgoCadenceManager
from genesis.ego.cell_promotion import handle_cell_promotion_resolution
from genesis.outreach.config import OutreachConfig, QuietHours
from genesis.outreach.governance import GovernanceGate
from genesis.outreach.pipeline import OutreachPipeline
from genesis.outreach.types import OutreachCategory, OutreachRequest, OutreachStatus

_CELL = {"domain": "email", "verb": "send", "risk_class": "standard"}


class _Rt:
    """Minimal runtime double for the resolution watcher (reads _db + pipeline)."""

    def __init__(self, db, pipeline):
        self._db = db
        self._outreach_pipeline = pipeline


@pytest.fixture
def config():
    return OutreachConfig(
        quiet_hours=QuietHours(start="22:00", end="07:00"),
        channel_preferences={"default": "email"},
        thresholds={"blocker": 0.0, "surplus": 0.0},
        max_daily=999, surplus_daily=999, content_daily=999, notification_daily=999,
        morning_report_time="07:00",
        engagement_timeout_hours=24, engagement_poll_minutes=60,
    )


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _pipeline(config, db, adapter):
    formatter = MagicMock()
    formatter.format.return_value = FormattedContent(text="re: hi", target=FormatTarget.EMAIL)
    pipe = OutreachPipeline(
        governance=GovernanceGate(config, db), drafter=AsyncMock(), formatter=formatter,
        channels={"email": adapter}, db=db, config=config,
        recipients={"email": "fallback@example.com"},
    )
    pipe.set_autonomy_gate(
        EmailAutonomyGate(db=db, approval_manager=ApprovalManager(db=db), event_bus=None)
    )
    return pipe


async def _add_inbound(db, thread, sender):
    await db.execute(
        "INSERT INTO email_thread_messages (thread_id, message_id, direction, sender, received_at) "
        "VALUES (?, ?, 'received', ?, ?)",
        (thread, f"m-{thread}", sender, "2026-06-21T00:00:00"),
    )
    await db.commit()


def _reply(recipient="alice@example.com", thread="t1"):
    return OutreachRequest(
        category=OutreachCategory.SURPLUS, topic="re: hi", context="body",
        salience_score=0.9, signal_type="email_reply", channel="email",
        validated_recipient=recipient, thread_id=thread,
    )


async def _approve_all_held_and_drain(db, rt):
    mgr = ApprovalManager(db=db)
    for row in await pes.list_held(db):
        await mgr.resolve(row["request_id"], status="approved")
    return await drain_pending_email_sends(rt)


@pytest.mark.asyncio
async def test_full_earn_grant_send_flag_demote_loop(config, db):
    adapter = AsyncMock()
    adapter.send_message.return_value = "<m@x>"
    pipe = _pipeline(config, db, adapter)
    rt = _Rt(db, pipe)
    await _add_inbound(db, "t1", "alice@example.com")  # known correspondent

    # 1. COLD START (all-ASK): the very first autonomous reply HOLDS, not sent.
    r = await pipe.submit_raw("re: hi", _reply())
    assert r.status == OutreachStatus.HELD
    adapter.send_message.assert_not_called()
    # the gate created the cell at ASK on first sight (CLASSIFY), not GRANTED
    assert (await cg.get_cell(db, **_CELL))["state"] == CellState.ASK.value

    # 2. EARN: owner approves 5 held replies → 5 successes on the standard cell.
    sent = await _approve_all_held_and_drain(db, rt)
    assert sent == 1  # the one held above, now approved+delivered
    for _ in range(4):
        await pipe.submit_raw("re: hi", _reply())
        await _approve_all_held_and_drain(db, rt)
    cell = await cg.get_cell(db, **_CELL)
    assert cell["successes"] == 5 and cell["state"] == CellState.ASK.value

    # 3. PROMOTABLE: the cell now qualifies for an owner-approved promotion.
    cands = await cg.detect_promotable_cells(db)
    assert [c["id"] for c in cands] == ["email:send:standard"]

    # 4. PROMOTE: build the real cadence proposal, owner approves it → GRANTED.
    prop = EgoCadenceManager._build_cell_promotion_proposal(cands[0])
    prop = {**prop, "id": "promo1", "expected_outputs": json.dumps(prop["expected_outputs"])}
    assert await handle_cell_promotion_resolution(db, prop, "approved") is True
    assert (await cg.get_cell(db, **_CELL))["state"] == CellState.GRANTED.value

    # 5. AUTONOMOUS SEND: a reply now goes out WITHOUT holding, and is logged.
    adapter.send_message.reset_mock()
    r = await pipe.submit_raw("re: hi", _reply())
    assert r.status == OutreachStatus.DELIVERED
    adapter.send_message.assert_called_once()
    log = await aes.list_recent(db)
    assert len(log) == 1 and log[0]["recipient"] == "alice@example.com"
    cell = await cg.get_cell(db, **_CELL)
    assert cell["successes"] == 5  # autonomous use is NOT a success signal
    assert cell["last_used_at"] is not None

    # 6. FLAG: owner flags the autonomous send as bad (the dashboard route's
    #    orchestration) → records a correction → the cell craters back to ASK.
    now = datetime.now(UTC).isoformat()
    flagged = await aes.mark_flagged(db, log[0]["id"], flagged_at=now)
    assert flagged is True
    state = await cg.record_correction(db, updated_at=now, **_CELL)
    assert state == CellState.ASK
    cell = await cg.get_cell(db, **_CELL)
    assert cell["state"] == CellState.ASK.value
    assert cell["weighted_corrections"] >= 1.0  # crater deepens re-earn

    # 7. Re-flag is idempotent: no second demotion path is taken.
    assert await aes.mark_flagged(db, log[0]["id"], flagged_at=now) is False
