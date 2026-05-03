"""Tests for genesis.memory.drift — DRIFT multi-mode retrieval."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.drift import (
    _global_primer,
    _identify_clusters,
    _rrf_fuse,
    drift_recall,
)


class TestRRFFuse:
    """Test the RRF fusion algorithm."""

    def test_single_list(self):
        result = _rrf_fuse([["a", "b", "c"]])
        assert result["a"] > result["b"] > result["c"]

    def test_two_lists_overlap(self):
        """Items appearing in multiple lists get higher scores."""
        result = _rrf_fuse([["a", "b", "c"], ["b", "a", "d"]])
        # 'b' appears in both lists (rank 2 + rank 1)
        # 'a' appears in both lists (rank 1 + rank 2)
        # They should be close but both higher than 'c' or 'd'
        assert result["a"] == result["b"]  # symmetric positions
        assert result["a"] > result["c"]
        assert result["a"] > result["d"]

    def test_empty_lists(self):
        result = _rrf_fuse([])
        assert result == {}

    def test_single_item(self):
        result = _rrf_fuse([["x"]])
        assert "x" in result
        assert result["x"] == 1.0 / (60 + 1)


class TestIdentifyClusters:
    """Test wing/room identification from memory IDs."""

    @pytest.mark.asyncio
    async def test_empty_ids(self):
        db = AsyncMock()
        wing, room = await _identify_clusters([], db=db)
        assert wing is None
        assert room is None

    @pytest.mark.asyncio
    async def test_identifies_dominant_wing(self):
        db = AsyncMock()
        # Mock the cursor as async context manager + async iterator
        mock_cursor = MagicMock()
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)

        # Simulate rows: 3 infrastructure, 1 learning
        rows = [
            ("id1", "infrastructure", "health"),
            ("id2", "infrastructure", "scheduler"),
            ("id3", "infrastructure", "health"),
            ("id4", "learning", "observations"),
        ]

        async def _aiter(self):
            for row in rows:
                yield row

        mock_cursor.__aiter__ = _aiter
        db.execute = MagicMock(return_value=mock_cursor)

        wing, room = await _identify_clusters(
            ["id1", "id2", "id3", "id4"], db=db
        )
        assert wing == "infrastructure"
        assert room == "health"  # Most common room


class TestGlobalPrimer:
    """Test the global primer phase."""

    @pytest.mark.asyncio
    async def test_fts_only_fallback(self):
        """When embeddings are unavailable, falls back to FTS5-only."""
        db = AsyncMock()
        qdrant = MagicMock()
        embeddings = AsyncMock()
        embeddings.embed = AsyncMock(side_effect=Exception("no embeddings"))

        with patch(
            "genesis.memory.drift.memory_crud.search_ranked",
            new_callable=AsyncMock,
            return_value=[
                {"memory_id": "m1", "content": "test", "rank": -1.0},
                {"memory_id": "m2", "content": "test2", "rank": -2.0},
            ],
        ), patch(
            "genesis.memory.drift._identify_clusters",
            new_callable=AsyncMock,
            return_value=("infrastructure", "health"),
        ):
            ids, wing, room = await _global_primer(
                "test query",
                db=db,
                qdrant_client=qdrant,
                embedding_provider=embeddings,
                source_collections=["episodic_memory"],
            )

        assert "m1" in ids
        assert "m2" in ids
        assert wing == "infrastructure"
        assert room == "health"


class TestDriftRecall:
    """Test the full DRIFT recall pipeline."""

    @pytest.mark.asyncio
    async def test_empty_results(self):
        """Returns empty list when no results found."""
        db = AsyncMock()
        qdrant = MagicMock()
        embeddings = AsyncMock()
        embeddings.embed = AsyncMock(side_effect=Exception("no embeddings"))

        with patch(
            "genesis.memory.drift.memory_crud.search_ranked",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results = await drift_recall(
                "nonexistent query",
                db=db,
                qdrant_client=qdrant,
                embedding_provider=embeddings,
            )

        assert results == []
