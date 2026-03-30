"""Tests for knowledge graph traversal."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.memory.graph import (
    find_connected_by_type,
    get_cluster,
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

    yield db
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


class TestFindConnectedByType:
    """Tests for type-filtered traversal."""

    @pytest.mark.asyncio
    async def test_filter_by_supports(self, graph_db):
        nodes = await find_connected_by_type(graph_db, "A", "supports")
        assert len(nodes) == 1
        assert nodes[0].memory_id == "B"
        assert nodes[0].link_type == "supports"

    @pytest.mark.asyncio
    async def test_filter_by_evaluated_for(self, graph_db):
        nodes = await find_connected_by_type(graph_db, "A", "evaluated_for")
        assert len(nodes) == 1
        assert nodes[0].memory_id == "D"

    @pytest.mark.asyncio
    async def test_no_matching_type(self, graph_db):
        nodes = await find_connected_by_type(graph_db, "A", "contradicts")
        assert nodes == []


class TestGetCluster:
    """Tests for bidirectional cluster discovery."""

    @pytest.mark.asyncio
    async def test_cluster_from_A(self, graph_db):
        cluster = await get_cluster(graph_db, "A", max_depth=2, min_strength=0.5)
        # A→B (0.8), A→D (0.7), B→C (0.9), B→E (0.6)
        assert "B" in cluster
        assert "D" in cluster

    @pytest.mark.asyncio
    async def test_cluster_from_leaf(self, graph_db):
        # From D, bidirectional should find A (since A→D exists)
        cluster = await get_cluster(graph_db, "D", max_depth=2, min_strength=0.5)
        assert "A" in cluster

    @pytest.mark.asyncio
    async def test_isolated_node(self, graph_db):
        cluster = await get_cluster(graph_db, "Z_isolated")
        assert cluster == []
