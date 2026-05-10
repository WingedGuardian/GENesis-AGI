"""Tests for the SVO event calendar: CRUD, temporal parser, and extraction integration."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from genesis.memory.extraction import parse_extraction_response_full
from genesis.memory.temporal import has_temporal_markers, parse_temporal_reference

# ---------------------------------------------------------------------------
# Temporal parser
# ---------------------------------------------------------------------------


class TestTemporalParser:
    """Tests for parse_temporal_reference()."""

    # Fixed reference point: Thursday 2026-05-08 12:00 UTC
    NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)

    def test_yesterday(self):
        result = parse_temporal_reference("what happened yesterday", now=self.NOW)
        assert result == ("2026-05-07", "2026-05-07")

    def test_today(self):
        result = parse_temporal_reference("what happened today", now=self.NOW)
        assert result == ("2026-05-08", "2026-05-08")

    def test_last_week(self):
        result = parse_temporal_reference("what happened last week", now=self.NOW)
        # Last week: Mon 2026-04-27 to Sun 2026-05-03
        assert result == ("2026-04-27", "2026-05-03")

    def test_this_week(self):
        result = parse_temporal_reference("what happened this week", now=self.NOW)
        # This week starts Mon 2026-05-04 (May 8 is Thursday)
        assert result == ("2026-05-04", "2026-05-08")

    def test_last_month(self):
        result = parse_temporal_reference("what happened last month", now=self.NOW)
        assert result == ("2026-04-01", "2026-04-30")

    def test_this_month(self):
        result = parse_temporal_reference("what did we deploy this month", now=self.NOW)
        assert result == ("2026-05-01", "2026-05-08")

    def test_n_days_ago(self):
        result = parse_temporal_reference("what happened 3 days ago", now=self.NOW)
        assert result == ("2026-05-05", "2026-05-05")

    def test_n_weeks_ago(self):
        result = parse_temporal_reference("what happened 2 weeks ago", now=self.NOW)
        start, end = result
        # 2 weeks ago from Thu May 8 → week starting Mon Apr 20
        assert start == "2026-04-20"
        assert end == "2026-04-26"

    def test_n_months_ago(self):
        result = parse_temporal_reference("what happened 2 months ago", now=self.NOW)
        assert result == ("2026-03-01", "2026-03-31")

    def test_no_temporal(self):
        result = parse_temporal_reference("tell me about Genesis", now=self.NOW)
        assert result is None

    def test_case_insensitive(self):
        result = parse_temporal_reference("What Happened YESTERDAY", now=self.NOW)
        assert result is not None

    def test_january_wrap(self):
        """Test month subtraction wrapping across year boundary."""
        jan = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        result = parse_temporal_reference("what happened 2 months ago", now=jan)
        assert result == ("2025-11-01", "2025-11-30")


class TestTemporalMarkers:
    """Tests for has_temporal_markers()."""

    def test_when_query(self):
        assert has_temporal_markers("when did we deploy?")

    def test_yesterday(self):
        assert has_temporal_markers("what happened yesterday")

    def test_days_ago(self):
        assert has_temporal_markers("3 days ago we decided")

    def test_timeline(self):
        assert has_temporal_markers("show me the timeline")

    def test_no_markers(self):
        assert not has_temporal_markers("tell me about Genesis")

    def test_last_time(self):
        assert has_temporal_markers("last time we tried this")


# ---------------------------------------------------------------------------
# Extraction SVO parsing
# ---------------------------------------------------------------------------


class TestExtractionSVO:
    """Tests for SVO event field parsing in extraction responses."""

    def test_event_fields_parsed(self):
        response = """```json
{
  "extractions": [
    {
      "content": "PR #773 was closed by maintainer",
      "type": "entity",
      "confidence": 0.9,
      "entities": ["PR #773"],
      "relationships": [],
      "temporal": "2026-05-03",
      "event": {"subject": "maintainer", "verb": "closed", "object": "PR #773"}
    }
  ],
  "session_keywords": ["PR"],
  "session_topic": "PR closure"
}```"""
        parsed = parse_extraction_response_full(response)
        assert len(parsed.extractions) == 1
        ext = parsed.extractions[0]
        assert ext.event_subject == "maintainer"
        assert ext.event_verb == "closed"
        assert ext.event_object == "PR #773"
        assert ext.temporal == "2026-05-03"

    def test_no_event_fields_when_absent(self):
        response = """```json
{
  "extractions": [
    {
      "content": "Genesis uses RRF fusion",
      "type": "concept",
      "confidence": 0.85,
      "entities": ["Genesis", "RRF"],
      "relationships": []
    }
  ],
  "session_keywords": [],
  "session_topic": ""
}```"""
        parsed = parse_extraction_response_full(response)
        assert len(parsed.extractions) == 1
        ext = parsed.extractions[0]
        assert ext.event_subject is None
        assert ext.event_verb is None
        assert ext.event_object is None

    def test_partial_event_fields(self):
        """Event with subject and verb but no object."""
        response = """```json
{
  "extractions": [
    {
      "content": "User approved the change",
      "type": "decision",
      "confidence": 0.8,
      "entities": [],
      "relationships": [],
      "temporal": "2026-05-07",
      "event": {"subject": "user", "verb": "approved"}
    }
  ],
  "session_keywords": [],
  "session_topic": ""
}```"""
        parsed = parse_extraction_response_full(response)
        ext = parsed.extractions[0]
        assert ext.event_subject == "user"
        assert ext.event_verb == "approved"
        assert ext.event_object is None

    def test_invalid_event_type_ignored(self):
        """Non-dict event field is silently ignored."""
        response = """```json
{
  "extractions": [
    {
      "content": "Something happened",
      "type": "entity",
      "confidence": 0.7,
      "entities": [],
      "relationships": [],
      "event": "not a dict"
    }
  ],
  "session_keywords": [],
  "session_topic": ""
}```"""
        parsed = parse_extraction_response_full(response)
        ext = parsed.extractions[0]
        assert ext.event_subject is None
        assert ext.event_verb is None


# ---------------------------------------------------------------------------
# CRUD (requires in-memory SQLite)
# ---------------------------------------------------------------------------


@pytest.fixture
async def memory_db():
    """Create an in-memory SQLite DB with memory_events table."""
    import aiosqlite
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("""
        CREATE TABLE memory_events (
            id                TEXT PRIMARY KEY,
            memory_id         TEXT NOT NULL,
            subject           TEXT NOT NULL,
            verb              TEXT NOT NULL,
            object            TEXT,
            event_date        TEXT,
            event_date_end    TEXT,
            confidence        REAL NOT NULL DEFAULT 0.5,
            source_session_id TEXT,
            created_at        TEXT NOT NULL
                              DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """)
    await db.execute(
        "CREATE INDEX idx_memory_events_date ON memory_events(event_date)"
    )
    await db.commit()
    yield db
    await db.close()


class TestMemoryEventsCRUD:
    """Tests for memory_events CRUD operations."""

    @pytest.mark.asyncio
    async def test_insert_and_query_by_date(self, memory_db):
        from genesis.db.crud import memory_events

        event_id = await memory_events.insert(
            memory_db,
            memory_id="mem-1",
            subject="user",
            verb="deployed",
            object_="PR #42",
            event_date="2026-05-03",
            confidence=0.9,
        )
        assert event_id  # non-empty string

        results = await memory_events.query_by_date_range(
            memory_db, "2026-05-01", "2026-05-05",
        )
        assert len(results) == 1
        assert results[0]["memory_id"] == "mem-1"
        assert results[0]["verb"] == "deployed"

    @pytest.mark.asyncio
    async def test_query_by_subject(self, memory_db):
        from genesis.db.crud import memory_events

        await memory_events.insert(
            memory_db,
            memory_id="mem-1",
            subject="Genesis",
            verb="evaluated",
            object_="FalkorDB",
            event_date="2026-05-03",
        )
        await memory_events.insert(
            memory_db,
            memory_id="mem-2",
            subject="user",
            verb="decided",
            event_date="2026-05-04",
        )

        results = await memory_events.query_by_subject(memory_db, "Genesis")
        assert len(results) == 1
        assert results[0]["memory_id"] == "mem-1"

    @pytest.mark.asyncio
    async def test_query_timeline(self, memory_db):
        from genesis.db.crud import memory_events

        await memory_events.insert(
            memory_db, memory_id="m1", subject="user",
            verb="merged", event_date="2026-05-01",
        )
        await memory_events.insert(
            memory_db, memory_id="m2", subject="user",
            verb="deployed", event_date="2026-05-03",
        )
        await memory_events.insert(
            memory_db, memory_id="m3", subject="Genesis",
            verb="evaluated", event_date="2026-05-02",
        )

        # All events, sorted by date desc
        timeline = await memory_events.query_timeline(memory_db)
        assert len(timeline) == 3
        assert timeline[0]["event_date"] == "2026-05-03"

        # Filter by verb
        merged = await memory_events.query_timeline(memory_db, verb="merged")
        assert len(merged) == 1

    @pytest.mark.asyncio
    async def test_get_memory_ids_in_range(self, memory_db):
        from genesis.db.crud import memory_events

        await memory_events.insert(
            memory_db, memory_id="m1", subject="user",
            verb="deployed", event_date="2026-05-01",
        )
        await memory_events.insert(
            memory_db, memory_id="m2", subject="user",
            verb="merged", event_date="2026-05-03",
        )
        # Same memory_id as m1 — should be deduped
        await memory_events.insert(
            memory_db, memory_id="m1", subject="user",
            verb="tested", event_date="2026-05-02",
        )

        ids = await memory_events.get_memory_ids_in_range(
            memory_db, "2026-05-01", "2026-05-03",
        )
        assert set(ids) == {"m1", "m2"}

    @pytest.mark.asyncio
    async def test_empty_range_returns_empty(self, memory_db):
        from genesis.db.crud import memory_events

        await memory_events.insert(
            memory_db, memory_id="m1", subject="user",
            verb="deployed", event_date="2026-05-01",
        )

        results = await memory_events.query_by_date_range(
            memory_db, "2026-06-01", "2026-06-30",
        )
        assert results == []
