"""Tests for dream cycle centrality recomputation phase."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.dream_centrality import run_centrality_recompute


@pytest.fixture
def phase_kwargs(db):
    return dict(
        qdrant=MagicMock(),
        db=db,
        router=AsyncMock(),
        store=AsyncMock(),
        run_id="test-run",
        dry_run=False,
    )


async def test_centrality_caches_scores(phase_kwargs):
    """Centrality scores are written to centrality_cache table."""
    mock_scores = [("mem-1", 0.45), ("mem-2", 0.32), ("mem-3", 0.18)]

    with patch(
        "genesis.memory.graph.centrality_scores",
        new_callable=AsyncMock,
        return_value=mock_scores,
    ):
        report = await run_centrality_recompute(**phase_kwargs)

    assert report["nodes_scored"] == 3
    assert report["top_score"] == 0.45

    db = phase_kwargs["db"]
    cursor = await db.execute("SELECT COUNT(*) FROM centrality_cache")
    assert (await cursor.fetchone())[0] == 3

    cursor = await db.execute(
        "SELECT centrality_score FROM centrality_cache WHERE memory_id = ?",
        ("mem-1",),
    )
    row = await cursor.fetchone()
    assert row[0] == pytest.approx(0.45, abs=1e-6)


async def test_centrality_replaces_on_rerun(phase_kwargs):
    """Second run replaces cache atomically."""
    with patch(
        "genesis.memory.graph.centrality_scores",
        new_callable=AsyncMock,
        return_value=[("mem-1", 0.5)],
    ):
        await run_centrality_recompute(**phase_kwargs)

    with patch(
        "genesis.memory.graph.centrality_scores",
        new_callable=AsyncMock,
        return_value=[("mem-2", 0.9)],
    ):
        await run_centrality_recompute(**phase_kwargs)

    db = phase_kwargs["db"]
    cursor = await db.execute("SELECT COUNT(*) FROM centrality_cache")
    assert (await cursor.fetchone())[0] == 1  # only mem-2, not both

    cursor = await db.execute("SELECT memory_id FROM centrality_cache")
    row = await cursor.fetchone()
    assert row[0] == "mem-2"


async def test_centrality_empty_graph(phase_kwargs):
    """Empty graph produces zero scores."""
    with patch(
        "genesis.memory.graph.centrality_scores",
        new_callable=AsyncMock,
        return_value=[],
    ):
        report = await run_centrality_recompute(**phase_kwargs)

    assert report["nodes_scored"] == 0


async def test_centrality_runs_in_dry_run(phase_kwargs):
    """Centrality runs even in dry_run since it's observational data."""
    phase_kwargs["dry_run"] = True

    with patch(
        "genesis.memory.graph.centrality_scores",
        new_callable=AsyncMock,
        return_value=[("mem-1", 0.3)],
    ):
        report = await run_centrality_recompute(**phase_kwargs)

    assert report["nodes_scored"] == 1

    db = phase_kwargs["db"]
    cursor = await db.execute("SELECT COUNT(*) FROM centrality_cache")
    assert (await cursor.fetchone())[0] == 1  # written even in dry_run
