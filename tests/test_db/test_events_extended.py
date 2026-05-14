"""Tests for extended events CRUD: pagination, min_severity, grouped errors."""

from __future__ import annotations

import pytest

from genesis.db.crud import events as crud


async def _seed_events(db, count=10):
    """Insert a spread of events for testing."""
    sevs = ["debug", "info", "warning", "error", "critical"]
    subs = ["routing", "awareness", "memory"]
    for i in range(count):
        await crud.insert(
            db,
            subsystem=subs[i % len(subs)],
            severity=sevs[i % len(sevs)],
            event_type=f"test.event.{i}",
            message=f"Test event number {i}",
            details={"index": i},
            timestamp=f"2026-03-14T{10 + i:02d}:00:00",
        )


# ── _severities_at_or_above ─────────────────────────────────────────────


class TestSeverityHelper:
    def test_from_warning(self):
        assert crud._severities_at_or_above("warning") == [
            "warning", "error", "critical",
        ]

    def test_from_debug(self):
        assert crud._severities_at_or_above("debug") == crud._SEVERITY_ORDER

    def test_from_critical(self):
        assert crud._severities_at_or_above("critical") == ["critical"]

    def test_unknown_returns_all(self):
        assert crud._severities_at_or_above("bogus") == crud._SEVERITY_ORDER


# ── query_paginated ─────────────────────────────────────────────────────


class TestQueryPaginated:
    @pytest.mark.asyncio
    async def test_basic_page(self, db):
        await _seed_events(db, 5)
        events, has_more = await crud.query_paginated(db, page_size=3)
        assert len(events) == 3
        assert has_more is True

    @pytest.mark.asyncio
    async def test_last_page(self, db):
        await _seed_events(db, 3)
        events, has_more = await crud.query_paginated(db, page_size=10)
        assert len(events) == 3
        assert has_more is False

    @pytest.mark.asyncio
    async def test_cursor_pagination(self, db):
        await _seed_events(db, 5)
        page1, _ = await crud.query_paginated(db, page_size=2)
        assert len(page1) == 2
        last = page1[-1]
        page2, _ = await crud.query_paginated(
            db, page_size=10,
            cursor_ts=last["timestamp"], cursor_id=last["id"],
        )
        assert len(page2) == 3
        # No overlap
        page1_ids = {e["id"] for e in page1}
        page2_ids = {e["id"] for e in page2}
        assert page1_ids.isdisjoint(page2_ids)

    @pytest.mark.asyncio
    async def test_min_severity_filter(self, db):
        await _seed_events(db, 10)
        events, _ = await crud.query_paginated(
            db, page_size=100, min_severity="warning",
        )
        for e in events:
            assert e["severity"] in ("warning", "error", "critical")

    @pytest.mark.asyncio
    async def test_subsystems_filter(self, db):
        await _seed_events(db, 10)
        events, _ = await crud.query_paginated(
            db, page_size=100, subsystems=["routing"],
        )
        for e in events:
            assert e["subsystem"] == "routing"

    @pytest.mark.asyncio
    async def test_search_filter(self, db):
        await _seed_events(db, 10)
        events, _ = await crud.query_paginated(
            db, page_size=100, search="number 3",
        )
        assert len(events) == 1
        assert "number 3" in events[0]["message"]

    @pytest.mark.asyncio
    async def test_details_deserialized(self, db):
        await _seed_events(db, 1)
        events, _ = await crud.query_paginated(db, page_size=10)
        assert isinstance(events[0]["details"], dict)

    @pytest.mark.asyncio
    async def test_empty(self, db):
        events, has_more = await crud.query_paginated(db, page_size=10)
        assert events == []
        assert has_more is False


# ── count_filtered ───────────────────────────────────────────────────────


class TestPrune:
    @pytest.mark.asyncio
    async def test_prune_all_types(self, db):
        """Without event_type filter, prune deletes all old events."""
        await crud.insert(
            db, subsystem="awareness", severity="info",
            event_type="heartbeat", message="alive",
            timestamp="2026-01-01T00:00:00",
        )
        await crud.insert(
            db, subsystem="routing", severity="error",
            event_type="breaker.tripped", message="down",
            timestamp="2026-01-01T00:00:00",
        )
        pruned = await crud.prune(db, older_than="2026-06-01T00:00:00")
        assert pruned == 2

    @pytest.mark.asyncio
    async def test_prune_by_event_type(self, db):
        """With event_type filter, only matching events are pruned."""
        await crud.insert(
            db, subsystem="awareness", severity="info",
            event_type="heartbeat", message="alive",
            timestamp="2026-01-01T00:00:00",
        )
        await crud.insert(
            db, subsystem="routing", severity="error",
            event_type="breaker.tripped", message="down",
            timestamp="2026-01-01T00:00:00",
        )
        pruned = await crud.prune(
            db, older_than="2026-06-01T00:00:00", event_type="heartbeat",
        )
        assert pruned == 1
        # The error event should remain
        remaining = await crud.count(db)
        assert remaining == 1

    @pytest.mark.asyncio
    async def test_prune_respects_timestamp(self, db):
        """Events newer than cutoff are kept."""
        await crud.insert(
            db, subsystem="awareness", severity="info",
            event_type="heartbeat", message="old",
            timestamp="2026-01-01T00:00:00",
        )
        await crud.insert(
            db, subsystem="awareness", severity="info",
            event_type="heartbeat", message="new",
            timestamp="2026-12-01T00:00:00",
        )
        pruned = await crud.prune(
            db, older_than="2026-06-01T00:00:00", event_type="heartbeat",
        )
        assert pruned == 1
        remaining = await crud.count(db)
        assert remaining == 1


class TestCountFiltered:
    @pytest.mark.asyncio
    async def test_total_count(self, db):
        await _seed_events(db, 10)
        assert await crud.count_filtered(db) == 10

    @pytest.mark.asyncio
    async def test_min_severity(self, db):
        await _seed_events(db, 10)
        # 10 events cycling debug/info/warning/error/critical → 2 each
        # warning+ = warning(2) + error(2) + critical(2) = 6
        ct = await crud.count_filtered(db, min_severity="warning")
        assert ct == 6

    @pytest.mark.asyncio
    async def test_subsystems(self, db):
        await _seed_events(db, 10)
        ct = await crud.count_filtered(db, subsystems=["routing"])
        assert ct > 0


# ── query_grouped_errors ────────────────────────────────────────────────


class TestQueryGroupedErrors:
    @pytest.mark.asyncio
    async def test_groups_warning_and_above(self, db):
        # Insert 3 warnings with same type, 2 errors with different type
        for i in range(3):
            await crud.insert(
                db, subsystem="routing", severity="warning",
                event_type="breaker.tripped",
                message="Provider down",
                timestamp=f"2026-03-14T10:{i:02d}:00",
            )
        for i in range(2):
            await crud.insert(
                db, subsystem="memory", severity="error",
                event_type="embed.failed",
                message="Embedding timeout",
                timestamp=f"2026-03-14T11:{i:02d}:00",
            )
        # Insert a debug event (should be excluded)
        await crud.insert(
            db, subsystem="routing", severity="debug",
            event_type="heartbeat", message="alive",
            timestamp="2026-03-14T12:00:00",
        )

        groups = await crud.query_grouped_errors(db)
        assert len(groups) == 2
        # Most recent group first
        assert groups[0]["subsystem"] == "memory"
        assert groups[0]["count"] == 2
        assert groups[1]["subsystem"] == "routing"
        assert groups[1]["count"] == 3

    @pytest.mark.asyncio
    async def test_since_filter(self, db):
        await crud.insert(
            db, subsystem="routing", severity="error",
            event_type="old", message="Old error",
            timestamp="2026-03-13T10:00:00",
        )
        await crud.insert(
            db, subsystem="routing", severity="error",
            event_type="new", message="New error",
            timestamp="2026-03-14T10:00:00",
        )
        groups = await crud.query_grouped_errors(db, since="2026-03-14T00:00:00")
        assert len(groups) == 1
        assert groups[0]["event_type"] == "new"

    @pytest.mark.asyncio
    async def test_subsystem_filter(self, db):
        await crud.insert(
            db, subsystem="routing", severity="warning",
            event_type="a", message="x", timestamp="2026-03-14T10:00:00",
        )
        await crud.insert(
            db, subsystem="memory", severity="warning",
            event_type="b", message="y", timestamp="2026-03-14T10:00:00",
        )
        groups = await crud.query_grouped_errors(db, subsystem="memory")
        assert len(groups) == 1
        assert groups[0]["subsystem"] == "memory"
