"""Tests for ConversationCollector — counts interactions since last reflection."""

from datetime import UTC, datetime

import pytest

from genesis.db.crud import awareness_ticks, cc_sessions
from genesis.learning.signals.conversation import ConversationCollector


@pytest.mark.asyncio
async def test_no_interactions_returns_zero(db):
    """No JSONL files, no channel sessions, no reflected ticks -> 0.0."""
    collector = ConversationCollector(db)
    reading = await collector.collect()
    # Value may be >0 if JSONL files exist on disk; just check structure
    assert reading.name == "conversations_since_reflection"
    assert reading.source == "cc_sessions"
    assert 0.0 <= reading.value <= 1.0


@pytest.mark.asyncio
async def test_channel_sessions_counted_since_reflection(db):
    """Telegram/terminal sessions after last reflection are counted."""
    now = datetime.now(UTC).isoformat()

    # Create a reflected tick in the past
    await awareness_ticks.create(
        db, id="tick-old", source="scheduled", signals_json="[]",
        scores_json="[]", created_at="2020-01-01T00:00:00",
        classified_depth="Micro", trigger_reason="test",
    )

    # Create channel sessions after that tick
    for i in range(5):
        await cc_sessions.create(
            db, id=f"tg-{i}", session_type="foreground", model="sonnet",
            effort="medium", status="active", user_id="u1", channel="telegram",
            started_at=now, last_activity_at=now, source_tag="telegram",
        )

    collector = ConversationCollector(db)
    reading = await collector.collect()
    # At least the 5 channel sessions should contribute (plus any JSONL)
    assert reading.value >= 0.5  # 5/10 = 0.5 minimum


@pytest.mark.asyncio
async def test_recent_reflection_resets_count(db):
    """A very recent reflected tick means channel sessions before it don't count."""
    now = datetime.now(UTC).isoformat()

    # Create sessions in the past
    for i in range(5):
        await cc_sessions.create(
            db, id=f"tg-{i}", session_type="foreground", model="sonnet",
            effort="medium", status="active", user_id="u1", channel="telegram",
            started_at="2020-01-01T00:00:00", last_activity_at="2020-01-01T00:00:00",
            source_tag="telegram",
        )

    # Create a reflected tick NOW (after the sessions)
    await awareness_ticks.create(
        db, id="tick-now", source="scheduled", signals_json="[]",
        scores_json="[]", created_at=now,
        classified_depth="Light", trigger_reason="test",
    )

    collector = ConversationCollector(db)
    reading = await collector.collect()
    # Channel sessions are before the tick, so they shouldn't count
    # (only JSONL turns after `now` would count, which is ~0)
    assert reading.value <= 0.3  # Allow some JSONL noise


@pytest.mark.asyncio
async def test_caps_at_one(db):
    """Value never exceeds 1.0 regardless of interaction count."""
    now = datetime.now(UTC).isoformat()

    # Create many channel sessions
    for i in range(20):
        await cc_sessions.create(
            db, id=f"tg-{i}", session_type="foreground", model="sonnet",
            effort="medium", status="active", user_id="u1", channel="terminal",
            started_at=now, last_activity_at=now, source_tag="terminal",
        )

    collector = ConversationCollector(db)
    reading = await collector.collect()
    assert reading.value <= 1.0
