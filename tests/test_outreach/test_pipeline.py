"""Tests for the outreach pipeline orchestrator."""

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.content.types import DraftRequest, DraftResult, FormatTarget, FormattedContent
from genesis.db.schema import create_all_tables
from genesis.outreach.config import OutreachConfig, QuietHours
from genesis.outreach.governance import GovernanceGate
from genesis.outreach.pipeline import OutreachPipeline
from genesis.outreach.types import (
    OutreachCategory,
    OutreachRequest,
    OutreachStatus,
)


@pytest.fixture
def config():
    return OutreachConfig(
        quiet_hours=QuietHours(start="02:00", end="02:30"),
        channel_preferences={"default": "telegram"},
        thresholds={"blocker": 0.0, "alert": 0.3, "surplus": 0.7, "digest": 0.0},
        max_daily=5,
        surplus_daily=1,
        content_daily=3,
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
def mock_drafter():
    drafter = AsyncMock()
    drafter.draft.return_value = DraftResult(
        content=FormattedContent(text="Drafted message", target=FormatTarget.TELEGRAM,
                                  truncated=False, original_length=15),
        model_used="test-model",
        raw_draft="Drafted message",
    )
    return drafter


@pytest.fixture
def mock_formatter():
    formatter = MagicMock()
    formatter.format.return_value = FormattedContent(
        text="Formatted message", target=FormatTarget.TELEGRAM,
        truncated=False, original_length=17,
    )
    return formatter


@pytest.fixture
def mock_channel():
    adapter = AsyncMock()
    adapter.send_message.return_value = "delivery-123"
    return adapter


@pytest.mark.asyncio
async def test_submit_surplus_allowed(config, db, mock_drafter, mock_formatter, mock_channel):
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Good insight",
        context="Relevant context",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    result = await pipeline.submit(req)
    assert result.status == OutreachStatus.DELIVERED
    assert result.delivery_id == "delivery-123"
    assert result.channel == "telegram"
    mock_channel.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_submit_governance_denied(config, db, mock_drafter, mock_formatter, mock_channel):
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Low salience",
        context="Not important",
        salience_score=0.3,
        signal_type="surplus_insight",
    )
    result = await pipeline.submit(req)
    assert result.status == OutreachStatus.REJECTED
    mock_channel.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_submit_urgent_bypasses_governance(config, db, mock_drafter, mock_formatter, mock_channel):
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.BLOCKER,
        topic="All APIs down",
        context="Critical failure",
        salience_score=1.0,
        signal_type="critical_failure",
    )
    result = await pipeline.submit_urgent(req)
    assert result.status == OutreachStatus.DELIVERED
    mock_channel.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_submit_records_in_db(config, db, mock_drafter, mock_formatter, mock_channel):
    from genesis.db.crud import outreach as outreach_crud

    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="DB test",
        context="Verify DB write",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    result = await pipeline.submit(req)
    row = await outreach_crud.get_by_id(db, result.outreach_id)
    assert row is not None
    assert row["channel"] == "telegram"
    assert row["delivered_at"] is not None
    assert row["delivery_id"] == "delivery-123"


@pytest.mark.asyncio
async def test_submit_raw_skips_governance_and_drafter(config, db, mock_drafter, mock_formatter, mock_channel):
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.BLOCKER,
        topic="Infrastructure Alert (batched)",
        context="Raw alert text",
        salience_score=1.0,
        signal_type="health_alert",
    )
    result = await pipeline.submit_raw("Pre-formatted alert text", req)
    assert result.status == OutreachStatus.DELIVERED
    assert result.delivery_id == "delivery-123"
    # Drafter should NOT be called
    mock_drafter.draft.assert_not_called()
    # Channel should receive the formatted text
    mock_channel.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_submit_raw_records_in_db(config, db, mock_drafter, mock_formatter, mock_channel):
    from genesis.db.crud import outreach as outreach_crud

    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.BLOCKER,
        topic="Infrastructure Alert (batched)",
        context="Raw alert text",
        salience_score=1.0,
        signal_type="health_alert",
    )
    result = await pipeline.submit_raw("Pre-formatted alert text", req)
    row = await outreach_crud.get_by_id(db, result.outreach_id)
    assert row is not None
    assert row["channel"] == "telegram"
    assert row["delivered_at"] is not None


@pytest.mark.asyncio
async def test_delivery_failure_defers(config, db, mock_drafter, mock_formatter):
    failing_channel = AsyncMock()
    failing_channel.send_message.side_effect = ConnectionError("Network down")
    mock_deferred = AsyncMock()

    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": failing_channel},
        deferred_queue=mock_deferred,
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Defer test",
        context="Will fail delivery",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    result = await pipeline.submit(req)
    assert result.status == OutreachStatus.FAILED
    mock_deferred.enqueue.assert_called_once()


@pytest.mark.asyncio
async def test_alert_submit_passes_system_prompt(config, db, mock_drafter, mock_formatter, mock_channel):
    """Alert/blocker drafts should include a system_prompt from OUTREACH_ALERT.md."""
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="Code audit: high finding in invoker.py",
        context="Exception handling issue",
        salience_score=0.8,
        signal_type="code_audit",
    )
    await pipeline.submit(req)
    mock_drafter.draft.assert_called_once()
    draft_request = mock_drafter.draft.call_args[0][0]
    assert isinstance(draft_request, DraftRequest)
    assert draft_request.system_prompt is not None
    assert "ONLY the final message" in draft_request.system_prompt
    assert draft_request.tone == "urgent"


@pytest.mark.asyncio
async def test_surplus_submit_no_system_prompt(config, db, mock_drafter, mock_formatter, mock_channel):
    """Non-urgent categories should NOT get the alert system prompt."""
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Interesting finding",
        context="Some context",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    await pipeline.submit(req)
    mock_drafter.draft.assert_called_once()
    draft_request = mock_drafter.draft.call_args[0][0]
    assert draft_request.system_prompt is None
    assert draft_request.tone == "conversational"
