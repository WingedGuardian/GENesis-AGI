"""Tests for dream cycle centrality recomputation phase."""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.dream_centrality import CentralityResult, _read_result, run_centrality_recompute


@pytest.fixture
async def phase_kwargs(tmp_path):
    from genesis.db.connection import connect_aiosqlite_rw

    async with connect_aiosqlite_rw(tmp_path / "centrality.db") as db:
        await db.execute("""CREATE TABLE centrality_cache (
            memory_id TEXT PRIMARY KEY, centrality_score REAL, computed_at TEXT
        )""")
        yield dict(
            qdrant=MagicMock(), db=db, router=AsyncMock(), store=AsyncMock(),
            run_id="test-run", dry_run=False,
        )


def scored(scores):
    rows = [row for row in scores if row[1] > 0]
    return CentralityResult(
        "2026-01-01T00:00:00+00:00",
        len(scores),
        scores[0] if scores else None,
        len(rows),
        rows,
    )


@pytest.mark.parametrize(
    ("run_id", "limit", "body"),
    [
        ("different-run", 1000, b'["bridge", 0.5]\n'),
        ("this-run", 1, b'["bridge", 0.5]\n'),
        ("this-run", 1000, b''),
        ("this-run", 1000, b'["other", 0.5]\n'),
        ("this-run", 1000, b'["bridge", 0.5]'),
    ],
)
def test_incomplete_or_mismatched_worker_output_is_refused(run_id, limit, body):
    header = {
        "version": 1, "run_id": "this-run", "snapshot_at": "2026-01-01T00:00:00+00:00",
        "nodes_scored": 3, "nodes_persisted": 1, "top": ["bridge", 0.5],
    }
    with pytest.raises(ValueError):
        _read_result(io.BytesIO(json.dumps(header).encode() + b"\n" + body), run_id, limit)


async def test_cancellation_delivered_during_publication_rollback(tmp_path, monkeypatch):
    from genesis.memory.dream_centrality import _publish

    path = tmp_path / "database"
    path.touch()
    rollback_started = asyncio.Event()
    release_rollback = asyncio.Event()
    writer = AsyncMock()
    writer.executemany.side_effect = OSError("insert failed")

    async def rollback():
        rollback_started.set()
        await release_rollback.wait()

    writer.rollback.side_effect = rollback

    @asynccontextmanager
    async def open_writer(*args, **kwargs):
        yield writer

    monkeypatch.setattr("genesis.memory.dream_centrality.connect_aiosqlite_rw", open_writer)
    task = asyncio.create_task(_publish(path, path.stat(), scored([("new", 0.5)])))
    await asyncio.wait_for(rollback_started.wait(), 1)
    task.cancel()
    release_rollback.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    writer.commit.assert_not_awaited()


async def test_worker_scores_every_visible_node(tmp_path):
    from genesis.db.connection import get_raw_db
    from genesis.memory.dream_centrality_worker import score_snapshot

    path = tmp_path / "database"
    async with get_raw_db(path) as db:
        await db.executescript("""
            CREATE TABLE memory_metadata (memory_id TEXT, invalid_at TEXT, deprecated INTEGER);
            CREATE TABLE memory_links (source_id TEXT, target_id TEXT, link_type TEXT, strength REAL);
        """)
        await db.executemany(
            "INSERT INTO memory_links VALUES (?, ?, 'test', 1)",
            [(f"n{i}", f"n{i + 1}") for i in range(0, 600, 2)],
        )
        await db.commit()
    _snapshot_at, scores = await score_snapshot(path)
    assert len(scores) == 600


async def test_worker_quota_respects_a_lower_configured_cpu_budget(tmp_path, monkeypatch):
    from genesis.hostmetrics.readings import Memory
    from genesis.memory import dream_centrality
    from genesis.memory.graphstore import GraphUnavailableError

    monkeypatch.setenv("GENESIS_RB_THRESHOLD_PCT", "40")
    monkeypatch.setattr(
        dream_centrality.readings, "read_memory", lambda: Memory(20 * 2**30, 16 * 2**30, "test"),
    )
    monkeypatch.setattr(dream_centrality.readings, "cpu_capacity", lambda: 2)
    launch = AsyncMock(return_value=MagicMock(wait=AsyncMock(return_value=4)))
    monkeypatch.setattr(dream_centrality.asyncio, "create_subprocess_exec", launch)
    with pytest.raises(GraphUnavailableError, match="exited 4"):
        await dream_centrality._run_worker(tmp_path / "graph.db", "quota", io.BytesIO())
    args = launch.call_args.args
    assert args[args.index("--cpu") + 1] == args[-1] == "64"


async def test_centrality_caches_scores(phase_kwargs):
    """Centrality scores are written to centrality_cache table."""
    mock_scores = [("mem-1", 0.45), ("mem-2", 0.32), ("mem-3", 0.18)]

    with patch(
        "genesis.memory.dream_centrality._run_worker",
        new_callable=AsyncMock,
        return_value=scored(mock_scores),
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
        "genesis.memory.dream_centrality._run_worker",
        new_callable=AsyncMock,
        return_value=scored([("mem-1", 0.5)]),
    ):
        await run_centrality_recompute(**phase_kwargs)

    with patch(
        "genesis.memory.dream_centrality._run_worker",
        new_callable=AsyncMock,
        return_value=scored([("mem-2", 0.9)]),
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
        "genesis.memory.dream_centrality._run_worker",
        new_callable=AsyncMock,
        return_value=scored([]),
    ):
        report = await run_centrality_recompute(**phase_kwargs)

    assert report["nodes_scored"] == 0


async def test_centrality_runs_in_dry_run(phase_kwargs):
    """Centrality runs even in dry_run since it's observational data."""
    phase_kwargs["dry_run"] = True

    with patch(
        "genesis.memory.dream_centrality._run_worker",
        new_callable=AsyncMock,
        return_value=scored([("mem-1", 0.3)]),
    ):
        report = await run_centrality_recompute(**phase_kwargs)

    assert report["nodes_scored"] == 1

    db = phase_kwargs["db"]
    cursor = await db.execute("SELECT COUNT(*) FROM centrality_cache")
    assert (await cursor.fetchone())[0] == 1  # written even in dry_run


async def test_centrality_skips_zero_scores(phase_kwargs):
    """Zero-betweenness nodes (the vast majority) are NOT persisted — otherwise
    a percentile over the cache degenerates to 0 and shields everything."""
    mock_scores = [("bridge", 0.42), ("z1", 0.0), ("z2", 0.0)]

    with patch(
        "genesis.memory.dream_centrality._run_worker",
        new_callable=AsyncMock,
        return_value=scored(mock_scores),
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
        "genesis.memory.dream_centrality._run_worker",
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
        "genesis.memory.dream_centrality._run_worker",
        new_callable=AsyncMock,
        return_value=scored([]),
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
    # would silently no-op, which is worse than failing loudly. VERIFIED at
    # merge time: graph.py has zero _NX_AVAILABLE references on this branch,
    # so main's form would have patched nothing.
    with (
        patch.object(graphstore_nx, "_NX_AVAILABLE", False),
        pytest.raises(graph_mod.GraphUnavailableError),
    ):
        await graph_mod.centrality_scores(db, top_n=None)


async def test_failed_publication_keeps_previous_cache(tmp_path):
    from genesis.db.connection import get_raw_db

    async with get_raw_db(tmp_path / "centrality.db") as conn:
        await conn.executescript("""
            CREATE TABLE centrality_cache (
                memory_id TEXT PRIMARY KEY,
                centrality_score REAL NOT NULL,
                computed_at TEXT NOT NULL
            );
            INSERT INTO centrality_cache VALUES ('prior', 0.7, '2026-01-01');
            CREATE TRIGGER reject_insert BEFORE INSERT ON centrality_cache
            WHEN NEW.memory_id = 'fail' BEGIN
                SELECT RAISE(ABORT, 'injected publication failure');
            END;
        """)
        with patch(
            "genesis.memory.dream_centrality._run_worker",
            new_callable=AsyncMock,
            return_value=scored([("new", 0.8), ("fail", 0.3)]),
        ):
            report = await run_centrality_recompute(
                db=conn, qdrant=MagicMock(), router=AsyncMock(), store=AsyncMock(),
                run_id="test-run", dry_run=True,
            )
        assert report["error"] == "injected publication failure"
        cursor = await conn.execute("SELECT memory_id FROM centrality_cache")
        assert [row[0] for row in await cursor.fetchall()] == ["prior"]
