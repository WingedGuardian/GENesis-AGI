"""Tests for morning report generator."""

from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.content.types import DraftResult, FormatTarget, FormattedContent
from genesis.db.schema import create_all_tables
from genesis.outreach.morning_report import MorningReportGenerator
from genesis.outreach.types import OutreachCategory


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def mock_health():
    health = AsyncMock()
    health.snapshot.return_value = {
        "timestamp": "2026-03-12T07:00:00Z",
        "cost": {"daily_usd": 1.23, "monthly_usd": 15.0, "budget_status": "ok"},
        "cc_sessions": {"foreground": 0, "background": {"active": 0}},
        "queues": {"deferred_work": 0, "dead_letters": 0},
        "infrastructure": {
            "genesis.db": {"status": "ok", "latency_ms": 1.2},
            "qdrant": {"status": "ok", "latency_ms": 2.1},
            "disk": {"status": "ok", "free_gb": 50.0},
        },
        "surplus": {"status": "idle", "queue_depth": 2},
    }
    return health


@pytest.fixture
def mock_drafter():
    drafter = AsyncMock()
    drafter.draft.return_value = DraftResult(
        content=FormattedContent(
            text="Good morning. System healthy, $1.23 spent today.",
            target=FormatTarget.GENERIC,
            truncated=False,
            original_length=47,
        ),
        model_used="gemini-free",
        raw_draft="Good morning. System healthy, $1.23 spent today.",
    )
    return drafter


@pytest.mark.asyncio
async def test_generate_returns_outreach_request(db, mock_health, mock_drafter):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    req = await gen.generate()
    assert req.category == OutreachCategory.DIGEST
    assert req.signal_type == "morning_report"
    assert req.salience_score == 0.0
    assert "morning" in req.topic.lower() or "report" in req.topic.lower()


@pytest.mark.asyncio
async def test_generate_calls_health_snapshot(db, mock_health, mock_drafter):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    mock_health.snapshot.assert_called_once()


@pytest.mark.asyncio
async def test_generate_calls_drafter(db, mock_health, mock_drafter):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    mock_drafter.draft.assert_called_once()
    call_args = mock_drafter.draft.call_args[0][0]
    assert "morning" in call_args.topic.lower() or "report" in call_args.topic.lower()


@pytest.mark.asyncio
async def test_generate_includes_health_in_context(db, mock_health, mock_drafter):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    call_args = mock_drafter.draft.call_args[0][0]
    assert "1.23" in call_args.context


@pytest.mark.asyncio
async def test_event_bus_emits_on_section_failure(db, mock_health, mock_drafter):
    """When a section fails, event_bus.emit should be called with WARNING."""
    event_bus = AsyncMock()
    # Make cognitive state query fail
    broken_db = AsyncMock()
    broken_db.execute = AsyncMock(side_effect=RuntimeError("DB gone"))

    # Use real health so _assemble_context reaches the failing DB sections
    mock_health.snapshot.return_value = {
        "cost": {}, "queues": {}, "infrastructure": {}, "surplus": {},
    }

    gen = MorningReportGenerator(mock_health, broken_db, mock_drafter, event_bus=event_bus)
    await gen.generate()

    # Should have emitted warnings for cognitive_state, pending_items, engagement_summary
    assert event_bus.emit.call_count >= 3
    sections_warned = {
        call.kwargs.get("section") or call[1].get("section", "")
        for call in event_bus.emit.call_args_list
    }
    # Check at least some expected sections
    assert len(sections_warned) >= 1


@pytest.mark.asyncio
async def test_no_event_bus_still_works(db, mock_health, mock_drafter):
    """Without event_bus, failures should not crash."""
    gen = MorningReportGenerator(mock_health, db, mock_drafter, event_bus=None)
    req = await gen.generate()
    assert req.category == OutreachCategory.DIGEST
