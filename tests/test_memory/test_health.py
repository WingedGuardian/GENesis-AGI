"""Tests for genesis.memory.health — algorithmic memory health checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.memory.health import (
    distribution_stats,
    full_health_report,
    growth_stats,
    orphan_stats,
)


def _ts(days_ago: int = 0) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


@pytest.mark.asyncio()
async def test_orphan_stats(empty_db):
    db = empty_db
    # Insert 3 memories: 2 old (orphan candidates), 1 recent
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("m1", _ts(days_ago=30)),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("m2", _ts(days_ago=14)),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("m3", _ts(days_ago=1)),
    )
    # m4: old, will be linked — should NOT be orphan
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("m4", _ts(days_ago=20)),
    )
    # Link m4 to m3 so m4 is NOT an orphan (m1 and m2 remain unlinked)
    await db.execute(
        "INSERT INTO memory_links (source_id, target_id, link_type, strength, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("m4", "m3", "related_to", 0.5, _ts()),
    )
    await db.commit()

    result = await orphan_stats(db, min_age_days=7)
    assert result["total_memories"] == 4
    # m1 (old, unlinked) and m2 (old, unlinked) => orphans; m3 too recent; m4 linked
    assert result["orphans"] == 2
    assert 0 < result["orphan_pct"] <= 100


@pytest.mark.asyncio()
async def test_distribution_stats(empty_db):
    db = empty_db
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, collection) VALUES (?, ?, ?)",
        ("m1", _ts(), "episodic_memory"),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, collection) VALUES (?, ?, ?)",
        ("m2", _ts(), "episodic_memory"),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, collection) VALUES (?, ?, ?)",
        ("m3", _ts(), "knowledge"),
    )
    await db.commit()

    result = await distribution_stats(db)
    assert result["total"] == 3
    assert result["by_collection"]["episodic_memory"] == 2
    assert result["by_collection"]["knowledge"] == 1
    assert isinstance(result["top_tags"], list)


@pytest.mark.asyncio()
async def test_growth_stats(empty_db):
    db = empty_db
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("recent", _ts(days_ago=0)),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("week_old", _ts(days_ago=5)),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("month_old", _ts(days_ago=20)),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("ancient", _ts(days_ago=60)),
    )
    await db.commit()

    result = await growth_stats(db)
    assert result["last_24h"] == 1
    assert result["last_7d"] == 2
    assert result["last_30d"] == 3
    assert isinstance(result["avg_per_day_7d"], float)
    assert result["avg_per_day_7d"] == round(2 / 7, 2)


@pytest.mark.asyncio()
async def test_full_health_report_no_qdrant(empty_db):
    db = empty_db
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("m1", _ts()),
    )
    await db.commit()

    report = await full_health_report(db, qdrant_client=None)
    assert "orphans" in report
    assert "distribution" in report
    assert "growth" in report
    assert report["duplicates"] is None
    assert report["distribution"]["total"] == 1
