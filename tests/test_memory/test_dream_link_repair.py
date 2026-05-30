"""Tests for dream cycle link repair phase."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from genesis.memory.dream_link_repair import run_link_repair


@pytest.fixture
def phase_kwargs(db):
    """Standard kwargs for phase functions, using real db."""
    from unittest.mock import AsyncMock, MagicMock

    return dict(
        qdrant=MagicMock(),
        db=db,
        router=AsyncMock(),
        store=AsyncMock(),
        run_id="test-run",
        dry_run=False,
    )


async def test_no_links(phase_kwargs):
    """No links in table → nothing to repair."""
    report = await run_link_repair(**phase_kwargs)
    assert report["links_checked"] == 0
    assert report["orphaned_removed"] == 0


async def test_no_orphans(phase_kwargs):
    """All links reference existing memories → no orphans."""
    db = phase_kwargs["db"]

    # Create memories
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, collection) VALUES (?, ?, ?)",
        ("m1", "2026-01-01", "episodic_memory"),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, collection) VALUES (?, ?, ?)",
        ("m2", "2026-01-01", "episodic_memory"),
    )
    # Create link between existing memories
    from genesis.db.crud import memory_links

    await memory_links.create(
        db, source_id="m1", target_id="m2", link_type="supports", created_at="2026-01-01",
    )
    await db.commit()

    report = await run_link_repair(**phase_kwargs)
    assert report["links_checked"] == 2  # m1 and m2 both referenced
    assert report["orphaned_removed"] == 0


async def test_removes_orphaned_links(phase_kwargs):
    """Links referencing nonexistent memories are removed."""
    db = phase_kwargs["db"]

    # Create only m1 (m2 does NOT exist in metadata)
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, collection) VALUES (?, ?, ?)",
        ("m1", "2026-01-01", "episodic_memory"),
    )
    # Create link to nonexistent m2
    await db.execute(
        "INSERT INTO memory_links (source_id, target_id, link_type, strength, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("m1", "ghost", "supports", 0.5, "2026-01-01"),
    )
    await db.commit()

    with patch("genesis.memory.graph.invalidate_graph_cache"):
        report = await run_link_repair(**phase_kwargs)

    assert report["orphaned_removed"] >= 1
    assert "ghost" in report["orphaned_ids"]


async def test_dry_run_reports_but_no_delete(phase_kwargs):
    """Dry run reports orphans but doesn't delete."""
    phase_kwargs["dry_run"] = True
    db = phase_kwargs["db"]

    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, collection) VALUES (?, ?, ?)",
        ("m1", "2026-01-01", "episodic_memory"),
    )
    await db.execute(
        "INSERT INTO memory_links (source_id, target_id, link_type, strength, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("m1", "ghost", "supports", 0.5, "2026-01-01"),
    )
    await db.commit()

    report = await run_link_repair(**phase_kwargs)
    assert report["orphaned_removed"] == 0
    assert report.get("would_remove", 0) > 0

    # Verify link still exists
    cursor = await db.execute("SELECT COUNT(*) FROM memory_links")
    row = await cursor.fetchone()
    assert row[0] == 1
