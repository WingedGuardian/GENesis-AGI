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


async def test_centrality_requests_all_nodes(phase_kwargs):
    """Widened persistence: centrality_scores is called with top_n=None so the
    full scored population (not just top-500) is available to the shield."""
    with patch(
        "genesis.memory.graph.centrality_scores",
        new_callable=AsyncMock,
        return_value=[("mem-1", 0.5)],
    ) as mock_scores:
        await run_centrality_recompute(**phase_kwargs)

    _, kwargs = mock_scores.call_args
    assert kwargs.get("top_n") is None


async def test_centrality_skips_zero_scores(phase_kwargs):
    """Zero-betweenness nodes (the vast majority) are NOT persisted — otherwise
    a percentile over the cache degenerates to 0 and shields everything."""
    mock_scores = [("bridge", 0.42), ("z1", 0.0), ("z2", 0.0)]

    with patch(
        "genesis.memory.graph.centrality_scores",
        new_callable=AsyncMock,
        return_value=mock_scores,
    ):
        report = await run_centrality_recompute(**phase_kwargs)

    assert report["nodes_scored"] == 3  # all computed
    assert report["nodes_persisted"] == 1  # only the nonzero one

    db = phase_kwargs["db"]
    cursor = await db.execute("SELECT memory_id FROM centrality_cache")
    rows = [r[0] for r in await cursor.fetchall()]
    assert rows == ["bridge"]


async def test_unavailable_graph_keeps_the_existing_cache(phase_kwargs):
    """"Store unreachable" must NOT masquerade as "no bridges exist".

    The fail-open chain this pins shut: centrality_scores returning [] on an
    unavailable backend read as an empty result, the recompute wiped
    centrality_cache, the importance shield computed no threshold, and
    bridge-node protection silently disappeared. Unavailability now raises,
    and the recompute must leave the previous cache STANDING.
    """
    from genesis.memory.graph import GraphUnavailableError

    db = phase_kwargs["db"]
    await db.execute(
        "INSERT INTO centrality_cache (memory_id, centrality_score, computed_at) "
        "VALUES ('mem-prior', 0.7, '2026-01-01T00:00:00+00:00')"
    )
    await db.commit()

    with patch(
        "genesis.memory.graph.centrality_scores",
        new_callable=AsyncMock,
        side_effect=GraphUnavailableError("backend down"),
    ):
        report = await run_centrality_recompute(**phase_kwargs)

    assert report.get("graph_unavailable") is True
    cursor = await db.execute("SELECT COUNT(*) FROM centrality_cache")
    assert (await cursor.fetchone())[0] == 1, (
        "an unavailable graph wiped the shield's threshold population"
    )


async def test_empty_graph_still_supersedes_the_cache(phase_kwargs):
    """CONTROL for the fix above: a REAL empty result (zero bridges) must keep
    clearing stale rows — the unavailability fix must not turn every empty
    run into a keep."""
    db = phase_kwargs["db"]
    await db.execute(
        "INSERT INTO centrality_cache (memory_id, centrality_score, computed_at) "
        "VALUES ('mem-stale', 0.7, '2026-01-01T00:00:00+00:00')"
    )
    await db.commit()

    with patch(
        "genesis.memory.graph.centrality_scores",
        new_callable=AsyncMock,
        return_value=[],
    ):
        report = await run_centrality_recompute(**phase_kwargs)

    assert not report.get("graph_unavailable")
    cursor = await db.execute("SELECT COUNT(*) FROM centrality_cache")
    assert (await cursor.fetchone())[0] == 0


async def test_centrality_scores_raises_when_networkx_absent(db):
    """The producer half of the contract: unavailability is a typed raise,
    never an empty list wearing "no bridges" clothing."""
    from genesis.memory import graph as graph_mod
    from genesis.memory import graphstore_nx

    # Patch where the flag LIVES (the NetworkX store), not where it used to.
    # graph.py deliberately does not re-export it: a patch aimed at the facade
    # would silently no-op, which is worse than failing loudly.
    with (
        patch.object(graphstore_nx, "_NX_AVAILABLE", False),
        pytest.raises(graph_mod.GraphUnavailableError),
    ):
        await graph_mod.centrality_scores(db, top_n=None)
