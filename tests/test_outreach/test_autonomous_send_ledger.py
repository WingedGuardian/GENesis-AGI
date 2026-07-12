"""WS-8 PR-D — the pipeline logs autonomous (GRANTED-cell) email sends to the
``autonomous_email_sends`` ledger, and ONLY those (not held sends, not the
owner-approved resume path)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.autonomy.approval import ApprovalManager
from genesis.autonomy.email_gate import EmailAutonomyGate
from genesis.autonomy.types import CellEvent
from genesis.content.types import FormatTarget, FormattedContent
from genesis.db.crud import autonomous_email_sends as aes
from genesis.db.crud import capability_grants as cg
from genesis.db.schema import create_all_tables
from genesis.outreach.config import OutreachConfig, QuietHours
from genesis.outreach.governance import GovernanceGate
from genesis.outreach.pipeline import OutreachPipeline
from genesis.outreach.types import OutreachCategory, OutreachRequest, OutreachStatus

_TS = "2026-06-21T00:00:00"


@pytest.fixture
def config():
    return OutreachConfig(
        quiet_hours=QuietHours(start="22:00", end="07:00"),
        channel_preferences={"default": "email"},
        thresholds={"blocker": 0.0, "surplus": 0.0},
        max_daily=50,
        surplus_daily=50,
        content_daily=50,
        notification_daily=50,
        morning_report_time="07:00",
        engagement_timeout_hours=24,
        engagement_poll_minutes=60,
    )


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def email_adapter():
    a = AsyncMock()
    a.send_message.return_value = "<msgid@example.com>"
    return a


def _pipeline(config, db, email_adapter):
    formatter = MagicMock()
    formatter.format.return_value = FormattedContent(
        text="re: hello",
        target=FormatTarget.EMAIL,
    )
    pipeline = OutreachPipeline(
        governance=GovernanceGate(config, db),
        drafter=AsyncMock(),
        formatter=formatter,
        channels={"email": email_adapter},
        db=db,
        config=config,
        recipients={"email": "fallback@example.com"},
    )
    pipeline.set_autonomy_gate(
        EmailAutonomyGate(db=db, approval_manager=ApprovalManager(db=db), event_bus=None)
    )
    return pipeline


async def _grant_standard(db):
    for ev in (CellEvent.CLASSIFY, CellEvent.APPROVE):
        await cg.apply_event(
            db,
            origin_class="first_party",
            domain="email",
            verb="send",
            risk_class="standard",
            event=ev,
            updated_at=_TS,
        )


async def _add_inbound(db, thread_id, sender="alice@example.com"):
    await db.execute(
        "INSERT INTO email_thread_messages "
        "(thread_id, message_id, direction, sender, received_at) "
        "VALUES (?, ?, 'received', ?, ?)",
        (thread_id, f"m-{thread_id}", sender, _TS),
    )
    await db.commit()


def _req():
    return OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="re: hello",
        context="body",
        salience_score=0.9,
        signal_type="email_reply",
        channel="email",
        validated_recipient="alice@example.com",
        thread_id="t1",
    )


@pytest.mark.asyncio
async def test_granted_send_is_logged_to_ledger(config, db, email_adapter):
    await _grant_standard(db)
    await _add_inbound(db, "t1")
    pipeline = _pipeline(config, db, email_adapter)

    result = await pipeline.submit_raw("re: hello", _req())

    assert result.status == OutreachStatus.DELIVERED
    sends = await aes.list_recent(db)
    assert len(sends) == 1
    assert sends[0]["recipient"] == "alice@example.com"
    assert sends[0]["cell_risk_class"] == "standard"
    assert sends[0]["thread_id"] == "t1"
    # last_used_at bumped, successes NOT incremented (autonomous use != competence)
    cell = await cg.get_cell(db, "email", "send", "standard")
    assert cell["last_used_at"] is not None
    assert cell["successes"] == 0


@pytest.mark.asyncio
async def test_held_send_is_not_logged(config, db, email_adapter):
    # standard cell NOT granted → held → nothing delivered, nothing logged.
    await _add_inbound(db, "t1")
    pipeline = _pipeline(config, db, email_adapter)

    result = await pipeline.submit_raw("re: hello", _req())

    assert result.status == OutreachStatus.HELD
    assert await aes.list_recent(db) == []
    email_adapter.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_resume_path_is_not_logged(config, db, email_adapter):
    # deliver_approved (gate_cleared=True) = an owner-APPROVED hold resuming →
    # NOT an autonomous send → must not be logged (P1-B exclusion).
    await _grant_standard(db)
    await _add_inbound(db, "t1")
    pipeline = _pipeline(config, db, email_adapter)
    pending = {
        "category": "surplus",
        "message": "approved reply",
        "validated_recipient": "alice@example.com",
        "thread_id": "t1",
    }

    result = await pipeline.deliver_approved(pending, subject="re: hello")

    assert result.status == OutreachStatus.DELIVERED
    email_adapter.send_message.assert_called_once()
    assert await aes.list_recent(db) == []  # resume is not autonomous
