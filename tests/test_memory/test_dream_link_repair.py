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


# ── Aged deprecated-edge prune (dream merge link rewiring, PR-B2) ─────────────

NOW = "2026-08-05T00:00:00+00:00"


async def _meta(db, mid, *, deprecated=0, run_id=None, created_at="2026-01-01T00:00:00+00:00"):
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, deprecated, dream_cycle_run_id) "
        "VALUES (?, ?, ?, ?)",
        (mid, created_at, deprecated, run_id),
    )
    await db.commit()


async def _link(db, s, t, lt="supports"):
    from genesis.db.crud import memory_links

    await memory_links.create(db, source_id=s, target_id=t, link_type=lt, created_at="2026-01-01")
    await db.commit()


async def test_prune_removes_aged_deprecated_edges_keeps_extends(phase_kwargs):
    """Edges of a long-deprecated dream original are pruned — except the
    synthesis->original 'extends' provenance link, which survives."""
    db = phase_kwargs["db"]
    await _meta(db, "orig", deprecated=1, run_id="R")
    # synthesis stamped synthesis:R, created long ago → aged past the 30d window
    await _meta(db, "synth", run_id="synthesis:R", created_at="2020-01-01T00:00:00+00:00")
    await _meta(db, "neighbor")
    await _link(db, "orig", "neighbor", "supports")  # stale → pruned
    await _link(db, "synth", "orig", "extends")  # provenance → kept

    report = await run_link_repair(**phase_kwargs, now=NOW)

    assert report["deprecated_edges_removed"] >= 1
    from genesis.db.crud import memory_links

    types = {link["link_type"] for link in await memory_links.get_links_for(db, "orig")}
    assert "supports" not in types
    assert "extends" in types


async def test_prune_keeps_young_deprecated_edges(phase_kwargs):
    """A recently-deprecated original (synthesis within the window) is NOT pruned
    — rollback must still be able to restore it with its edges."""
    db = phase_kwargs["db"]
    await _meta(db, "orig", deprecated=1, run_id="R")
    await _meta(db, "synth", run_id="synthesis:R", created_at="2026-08-01T00:00:00+00:00")  # 4d old
    await _meta(db, "neighbor")
    await _link(db, "orig", "neighbor", "supports")

    report = await run_link_repair(**phase_kwargs, now=NOW)

    assert report["deprecated_edges_removed"] == 0


async def test_prune_ignores_deprecated_without_run_id(phase_kwargs):
    """Entity-adjudication-style deprecations (deprecated=1, run_id NULL) are not
    dream merges — never pruned by this phase (status quo)."""
    db = phase_kwargs["db"]
    await _meta(db, "ea", deprecated=1, run_id=None)
    await _meta(db, "neighbor")
    await _link(db, "ea", "neighbor", "supports")

    report = await run_link_repair(**phase_kwargs, now=NOW)

    assert report["deprecated_edges_removed"] == 0


async def test_prune_dry_run_reports_without_deleting(phase_kwargs):
    db = phase_kwargs["db"]
    phase_kwargs["dry_run"] = True
    await _meta(db, "orig", deprecated=1, run_id="R")
    await _meta(db, "synth", run_id="synthesis:R", created_at="2020-01-01T00:00:00+00:00")
    await _meta(db, "neighbor")
    await _link(db, "orig", "neighbor", "supports")

    report = await run_link_repair(**phase_kwargs, now=NOW)

    assert report["would_remove_deprecated"] >= 1
    assert report["deprecated_edges_removed"] == 0
    cursor = await db.execute("SELECT COUNT(*) FROM memory_links")
    assert (await cursor.fetchone())[0] == 1  # not deleted
