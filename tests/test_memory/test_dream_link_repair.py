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


OLD = "2020-01-01T00:00:00+00:00"  # well past the 30d window
RECENT = "2026-08-01T00:00:00+00:00"  # 4 days before NOW → within the window


async def _meta(db, mid, *, deprecated=0, run_id=None, deprecated_at=None):
    await db.execute(
        "INSERT INTO memory_metadata "
        "(memory_id, created_at, deprecated, dream_cycle_run_id, deprecated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (mid, "2026-01-01T00:00:00+00:00", deprecated, run_id, deprecated_at),
    )
    await db.commit()


async def _link(db, s, t, lt="supports"):
    from genesis.db.crud import memory_links

    await memory_links.create(db, source_id=s, target_id=t, link_type=lt, created_at="2026-01-01")
    await db.commit()


async def test_prune_removes_aged_edges_keeps_only_synthesis_provenance(phase_kwargs):
    """A long-deprecated original's edges are pruned — including an ORDINARY
    ``extends`` edge (auto_link uses extends for high similarity) — but the
    synthesis->original provenance ``extends`` survives."""
    db = phase_kwargs["db"]
    await _meta(db, "orig", deprecated=1, run_id="R", deprecated_at=OLD)
    await _meta(db, "synth", run_id="synthesis:R")  # the live synthesis
    await _meta(db, "neighbor")
    await _meta(db, "other")
    await _link(db, "orig", "neighbor", "supports")  # ordinary → pruned
    await _link(db, "synth", "orig", "extends")  # provenance → kept
    await _link(db, "orig", "other", "extends")  # ORDINARY extends → pruned (P2)

    report = await run_link_repair(**phase_kwargs, now=NOW)

    assert report["deprecated_edges_removed"] == 2  # supports + ordinary extends
    from genesis.db.crud import memory_links

    remaining = {
        (link["source_id"], link["target_id"], link["link_type"])
        for link in await memory_links.get_links_for(db, "orig")
    }
    assert ("synth", "orig", "extends") in remaining  # provenance survives
    assert ("orig", "neighbor", "supports") not in remaining
    assert ("orig", "other", "extends") not in remaining  # ordinary extends pruned


async def test_prune_keeps_young_deprecated_edges(phase_kwargs):
    """A recently-deprecated original (within the window) is NOT pruned —
    rollback must still be able to restore it with its edges."""
    db = phase_kwargs["db"]
    await _meta(db, "orig", deprecated=1, run_id="R", deprecated_at=RECENT)
    await _meta(db, "neighbor")
    await _link(db, "orig", "neighbor", "supports")

    report = await run_link_repair(**phase_kwargs, now=NOW)

    assert report["deprecated_edges_removed"] == 0


async def test_prune_ignores_deprecated_without_deprecated_at(phase_kwargs):
    """Non-dream deprecations (deprecated=1 but deprecated_at NULL, e.g. entity
    adjudication) are never pruned by this phase."""
    db = phase_kwargs["db"]
    await _meta(db, "ea", deprecated=1, run_id=None, deprecated_at=None)
    await _meta(db, "neighbor")
    await _link(db, "ea", "neighbor", "supports")

    report = await run_link_repair(**phase_kwargs, now=NOW)

    assert report["deprecated_edges_removed"] == 0


async def test_prune_dry_run_reports_without_deleting(phase_kwargs):
    db = phase_kwargs["db"]
    phase_kwargs["dry_run"] = True
    await _meta(db, "orig", deprecated=1, run_id="R", deprecated_at=OLD)
    await _meta(db, "neighbor")
    await _link(db, "orig", "neighbor", "supports")

    report = await run_link_repair(**phase_kwargs, now=NOW)

    assert report["would_remove_deprecated"] >= 1
    assert report["deprecated_edges_removed"] == 0
    cursor = await db.execute("SELECT COUNT(*) FROM memory_links")
    assert (await cursor.fetchone())[0] == 1  # not deleted
