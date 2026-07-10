"""Tests for knowledge graph traversal."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.memory.graph import (
    centrality_scores,
    invalidate_graph_cache,
    traverse,
)


@pytest.fixture
async def graph_db(tmp_path):
    """Create an in-memory DB with memory_links table and test data."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row

    await db.execute("""
        CREATE TABLE memory_links (
            source_id   TEXT NOT NULL,
            target_id   TEXT NOT NULL,
            link_type   TEXT NOT NULL,
            strength    REAL NOT NULL DEFAULT 0.5,
            created_at  TEXT NOT NULL,
            PRIMARY KEY (source_id, target_id)
        )
    """)
    await db.execute(
        "CREATE INDEX idx_ml_source ON memory_links(source_id)"
    )
    await db.execute(
        "CREATE INDEX idx_ml_target ON memory_links(target_id)"
    )

    # Build a small test graph:
    #   A --supports(0.8)--> B --extends(0.9)--> C
    #   A --evaluated_for(0.7)--> D
    #   B --related_to(0.6)--> E
    #   E --supports(0.4)--> F  (weak link)
    links = [
        ("A", "B", "supports", 0.8),
        ("B", "C", "extends", 0.9),
        ("A", "D", "evaluated_for", 0.7),
        ("B", "E", "related_to", 0.6),
        ("E", "F", "supports", 0.4),
    ]
    for src, tgt, lt, strength in links:
        await db.execute(
            "INSERT INTO memory_links VALUES (?, ?, ?, ?, '2026-03-23')",
            (src, tgt, lt, strength),
        )
    await db.commit()

    # Ensure fresh NetworkX cache per test
    invalidate_graph_cache()

    yield db

    # Clean up cache so subsequent test files don't see stale state
    invalidate_graph_cache()
    await db.close()


class TestTraverse:
    """Tests for recursive CTE traversal."""

    @pytest.mark.asyncio
    async def test_depth_1(self, graph_db):
        result = await traverse(graph_db, "A", max_depth=1)
        ids = {n.memory_id for n in result.nodes}
        assert ids == {"B", "D"}
        assert result.query_ms >= 0

    @pytest.mark.asyncio
    async def test_depth_2(self, graph_db):
        result = await traverse(graph_db, "A", max_depth=2)
        ids = {n.memory_id for n in result.nodes}
        assert "B" in ids
        assert "C" in ids  # B→C at depth 2
        assert "D" in ids
        assert "E" in ids  # B→E at depth 2

    @pytest.mark.asyncio
    async def test_depth_3(self, graph_db):
        result = await traverse(graph_db, "A", max_depth=3)
        ids = {n.memory_id for n in result.nodes}
        assert "F" in ids  # E→F at depth 3

    @pytest.mark.asyncio
    async def test_min_strength_filter(self, graph_db):
        result = await traverse(graph_db, "A", max_depth=3, min_strength=0.5)
        ids = {n.memory_id for n in result.nodes}
        # F is connected via E→F with strength 0.4, should be filtered
        assert "F" not in ids
        assert "B" in ids
        assert "C" in ids

    @pytest.mark.asyncio
    async def test_no_links_returns_empty(self, graph_db):
        result = await traverse(graph_db, "Z_nonexistent")
        assert result.nodes == []
        assert result.root_id == "Z_nonexistent"

    @pytest.mark.asyncio
    async def test_cycle_prevention(self, graph_db):
        # Add a cycle: C → A
        await graph_db.execute(
            "INSERT INTO memory_links VALUES ('C', 'A', 'related_to', 0.8, '2026-03-23')",
        )
        await graph_db.commit()

        # Should not infinite loop
        result = await traverse(graph_db, "A", max_depth=5)
        # A should not appear in results (it's the root)
        ids = [n.memory_id for n in result.nodes]
        # The cycle is prevented by the path check
        assert len(ids) == len(set(ids))  # No duplicates


class TestNetworkXCache:
    """Tests for the NetworkX cache lifecycle."""

    @pytest.mark.asyncio
    async def test_cache_invalidation_triggers_rebuild(self, graph_db):
        """After invalidation, next query should still return correct results."""
        result1 = await traverse(graph_db, "A", max_depth=1)
        ids1 = {n.memory_id for n in result1.nodes}

        # Invalidate and re-query
        invalidate_graph_cache()
        result2 = await traverse(graph_db, "A", max_depth=1)
        ids2 = {n.memory_id for n in result2.nodes}

        assert ids1 == ids2 == {"B", "D"}

    @pytest.mark.asyncio
    async def test_cache_reflects_new_links(self, graph_db):
        """After adding a link and invalidating, the new link should appear."""
        result_before = await traverse(graph_db, "F", max_depth=1)
        assert result_before.nodes == []

        # Add a new link from F
        await graph_db.execute(
            "INSERT INTO memory_links VALUES ('F', 'A', 'related_to', 0.9, '2026-03-23')",
        )
        await graph_db.commit()
        invalidate_graph_cache()

        result_after = await traverse(graph_db, "F", max_depth=1)
        ids = {n.memory_id for n in result_after.nodes}
        assert "A" in ids


class TestCentrality:
    """Tests for betweenness centrality scoring."""

    @pytest.mark.asyncio
    async def test_centrality_returns_scores(self, graph_db):
        # Ensure cache is built
        invalidate_graph_cache()
        scores = await centrality_scores(graph_db, top_n=5)
        assert len(scores) > 0
        # Scores are (memory_id, float) tuples
        assert all(isinstance(s[0], str) and isinstance(s[1], float) for s in scores)

    @pytest.mark.asyncio
    async def test_centrality_bridge_node_ranks_high(self, graph_db):
        """B is a bridge between A→C and A→E paths — should rank high."""
        invalidate_graph_cache()
        scores = await centrality_scores(graph_db, top_n=10)
        score_by_id = dict(scores)
        # B connects to C and E — should have non-zero centrality
        assert score_by_id.get("B", 0.0) > 0.0
