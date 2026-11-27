"""Run the weekly centrality phase against an isolated SQLite database."""

import asyncio
import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.db.connection import get_raw_db
from genesis.hostmetrics.readings import Memory
from genesis.memory import dream_centrality
from genesis.memory.graphstore_nx import NetworkxGraphStore

pytestmark = [
    pytest.mark.user_manager,
    pytest.mark.real_tool("systemctl"),
    pytest.mark.skipif(
        os.environ.get("GENESIS_DREAM_NATIVE_E2E") != "1",
        reason="uses an actual systemd resource scope",
    ),
]


async def test_capped_child_to_cache(tmp_path, monkeypatch):
    database = tmp_path / "graph.db"
    fixture = os.environ.get("GENESIS_DREAM_NATIVE_FIXTURE")
    if fixture:
        shutil.copyfile(fixture, database)
    monkeypatch.setenv("PYTHONPATH", str(Path(dream_centrality.__file__).resolve().parents[2]))
    monkeypatch.setattr(
        dream_centrality.readings, "read_memory", lambda: Memory(15 * 2**30, 6 * 2**30, "test"),
    )
    monkeypatch.setattr(dream_centrality.readings, "cpu_capacity", lambda: 0.8)
    async with get_raw_db(database) as db:
        if fixture:
            expected = dict(await db.execute_fetchall(
                "SELECT memory_id, centrality_score FROM centrality_cache",
            ))
            assert len(expected) > 500
            await db.execute("DELETE FROM centrality_cache")
            await db.commit()
        else:
            await _seed_graph(db)
            expected = {
                identifier: round(score, 8)
                for identifier, score in await NetworkxGraphStore().centrality(db, top_n=None)
                if score > 0
            }

        phase = asyncio.create_task(dream_centrality.run_centrality_recompute(
            qdrant=MagicMock(), db=db, router=AsyncMock(), store=AsyncMock(),
            run_id="native-test", dry_run=True,
        ))
        probes = 0
        try:
            while not phase.done():
                started = asyncio.get_running_loop().time()
                await asyncio.sleep(0.05 if not fixture else 0.5)
                current = await asyncio.wait_for(
                    db.execute_fetchall("SELECT COUNT(*) FROM centrality_cache"), timeout=1,
                )
                assert current[0][0] in (0, len(expected))
                assert asyncio.get_running_loop().time() - started < 1.5
                probes += 1
            report = await phase
        finally:
            if not phase.done():
                phase.cancel()
                await asyncio.gather(phase, return_exceptions=True)
        assert "error" not in report, report
        assert probes >= 2
        assert report["nodes_scored"] >= (125_000 if fixture else 230)
        actual = dict(await db.execute_fetchall(
            "SELECT memory_id, centrality_score FROM centrality_cache",
        ))
        if actual != expected:
            pytest.fail("Published scores differ from the NetworkX baseline")


async def _seed_graph(db):
    await db.executescript("""
            CREATE TABLE memory_links (
                source_id TEXT, target_id TEXT, link_type TEXT, strength REAL
            );
            CREATE TABLE memory_metadata (memory_id TEXT, invalid_at TEXT, deprecated INTEGER);
            CREATE TABLE centrality_cache (
                memory_id TEXT PRIMARY KEY, centrality_score REAL, computed_at TEXT
            );
            INSERT INTO memory_metadata VALUES
                ('expired', '2025-01-01T00:00:00+00:00', 0), ('deprecated', NULL, 1);
            INSERT INTO memory_links VALUES
                ('n2', 'n3', 'typed-edge', 0.7), ('expired', 'n3', 'type', 1),
                ('n3', 'deprecated', 'type', 1);
    """)
    await db.executemany(
        "INSERT INTO memory_links VALUES (?, ?, 'type', 1)",
        [(f"n{i}", f"n{i + 1}") for i in range(229)],
    )
    await db.commit()
