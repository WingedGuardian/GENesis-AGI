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
async def test_context_includes_session_topics(db, mock_health, mock_drafter):
    """Session topics from foreground sessions appear in the activity context."""
    await db.execute(
        "INSERT INTO cc_sessions (id, session_type, model, effort, status, "
        "started_at, last_activity_at, topic) VALUES (?, ?, ?, ?, ?, "
        "datetime('now', '-2 hours'), datetime('now', '-1 hour'), ?)",
        ("s1", "foreground", "opus", "high", "completed",
         "Working on memory supersession chain"),
    )
    await db.commit()

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    call_args = mock_drafter.draft.call_args[0][0]
    assert "memory supersession" in call_args.context.lower()
    assert "Session topics" in call_args.context


@pytest.mark.asyncio
async def test_context_includes_user_goals(db, mock_health, mock_drafter):
    """Active user goals appear in the activity context for drift detection."""
    await db.execute(
        "INSERT INTO user_goals (id, title, category, priority, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, "
        "datetime('now', '-7 days'), datetime('now'))",
        ("g1", "W2 employment", "career", "high", "active"),
    )
    await db.commit()

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    call_args = mock_drafter.draft.call_args[0][0]
    assert "W2 employment" in call_args.context
    assert "Active user goals" in call_args.context


@pytest.mark.asyncio
async def test_background_sessions_excluded_from_topics(db, mock_health, mock_drafter):
    """Background sessions should NOT appear in session topics."""
    await db.execute(
        "INSERT INTO cc_sessions (id, session_type, model, effort, status, "
        "started_at, last_activity_at, topic) VALUES (?, ?, ?, ?, ?, "
        "datetime('now', '-2 hours'), datetime('now', '-1 hour'), ?)",
        ("bg1", "background_reflection", "sonnet", "medium", "completed",
         "Internal reflection cycle"),
    )
    await db.commit()

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    call_args = mock_drafter.draft.call_args[0][0]
    assert "Internal reflection cycle" not in call_args.context


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
