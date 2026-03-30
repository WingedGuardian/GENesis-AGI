"""Tests for awareness_ticks CRUD module."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from genesis.db.crud import awareness_ticks


@pytest.fixture
def tick_row():
    return {
        "id": "tick-001",
        "source": "scheduled",
        "signals_json": json.dumps([{"name": "software_error_spike", "value": 0.8}]),
        "scores_json": json.dumps([{"depth": "Micro", "final_score": 0.9, "triggered": True}]),
        "classified_depth": "Micro",
        "trigger_reason": "error spike",
        "created_at": datetime.now(UTC).isoformat(),
    }


async def test_create_and_get(db, tick_row):
    await awareness_ticks.create(db, **tick_row)
    row = await awareness_ticks.get_by_id(db, "tick-001")
    assert row is not None
    assert row["source"] == "scheduled"
    assert row["classified_depth"] == "Micro"


async def test_get_nonexistent(db):
    row = await awareness_ticks.get_by_id(db, "nope")
    assert row is None


async def test_query_by_depth(db, tick_row):
    await awareness_ticks.create(db, **tick_row)
    rows = await awareness_ticks.query(db, classified_depth="Micro")
    assert len(rows) == 1
    assert rows[0]["id"] == "tick-001"


async def test_query_by_source(db, tick_row):
    await awareness_ticks.create(db, **tick_row)
    rows = await awareness_ticks.query(db, source="scheduled")
    assert len(rows) == 1


async def test_count_in_window(db, tick_row):
    """Count ticks at a given depth within a time window."""
    await awareness_ticks.create(db, **tick_row)
    count = await awareness_ticks.count_in_window(
        db, depth="Micro", window_seconds=3600
    )
    assert count == 1


async def test_count_in_window_empty(db):
    count = await awareness_ticks.count_in_window(
        db, depth="Micro", window_seconds=3600
    )
    assert count == 0


async def test_count_in_window_excludes_old_ticks(db):
    """Tick from 2h ago must NOT appear in a 1h window (ISO timestamp regression)."""
    old_tick = {
        "id": "tick-old",
        "source": "scheduled",
        "signals_json": "[]",
        "scores_json": "[]",
        "classified_depth": "Micro",
        "trigger_reason": "test",
        "created_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    }
    await awareness_ticks.create(db, **old_tick)
    count = await awareness_ticks.count_in_window(
        db, depth="Micro", window_seconds=3600,
    )
    assert count == 0, "Old tick should NOT be counted in 1h window"


async def test_count_in_window_includes_recent_tick(db):
    """Tick from 10 minutes ago MUST appear in a 1h window."""
    recent_tick = {
        "id": "tick-recent",
        "source": "scheduled",
        "signals_json": "[]",
        "scores_json": "[]",
        "classified_depth": "Light",
        "trigger_reason": "test",
        "created_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
    }
    await awareness_ticks.create(db, **recent_tick)
    count = await awareness_ticks.count_in_window(
        db, depth="Light", window_seconds=3600,
    )
    assert count == 1, "Recent tick must be counted in 1h window"


async def test_count_in_window_all_excludes_old(db):
    """count_in_window_all must also respect ISO timestamps."""
    old_tick = {
        "id": "tick-all-old",
        "source": "scheduled",
        "signals_json": "[]",
        "scores_json": "[]",
        "classified_depth": "Deep",
        "trigger_reason": "test",
        "created_at": (datetime.now(UTC) - timedelta(hours=25)).isoformat(),
    }
    await awareness_ticks.create(db, **old_tick)
    count = await awareness_ticks.count_in_window_all(
        db, window_seconds=86400,
    )
    assert count == 0, "25h-old tick should not appear in 24h window"


async def test_count_by_source_excludes_old(db):
    """count_by_source must also respect ISO timestamps."""
    old_tick = {
        "id": "tick-src-old",
        "source": "scheduled",
        "signals_json": "[]",
        "scores_json": "[]",
        "classified_depth": None,
        "trigger_reason": None,
        "created_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    }
    await awareness_ticks.create(db, **old_tick)
    count = await awareness_ticks.count_by_source(
        db, source="scheduled", window_seconds=3600,
    )
    assert count == 0, "Old tick should not appear in 1h source window"


async def test_last_tick_at_depth(db, tick_row):
    await awareness_ticks.create(db, **tick_row)
    last = await awareness_ticks.last_at_depth(db, "Micro")
    assert last is not None
    assert last["id"] == "tick-001"


async def test_last_tick_at_depth_empty(db):
    last = await awareness_ticks.last_at_depth(db, "Deep")
    assert last is None


async def test_delete(db, tick_row):
    await awareness_ticks.create(db, **tick_row)
    deleted = await awareness_ticks.delete(db, "tick-001")
    assert deleted is True
    assert await awareness_ticks.get_by_id(db, "tick-001") is None
