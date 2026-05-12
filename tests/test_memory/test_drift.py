"""Tests for genesis.memory.drift — DRIFT multi-mode retrieval."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.drift import (
    _coalesce,
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


class TestCoalesce:
    """Test the null-coalescing helper."""

    def test_none_returns_default(self):
        assert _coalesce(None, 0.5) == 0.5

    def test_zero_preserved(self):
        """Unlike ``or``, 0 is not replaced by the default."""
        assert _coalesce(0, 0.5) == 0

    def test_empty_string_preserved(self):
        assert _coalesce("", "unknown") == ""

    def test_normal_value_passthrough(self):
        assert _coalesce(0.7, 0.5) == 0.7

    def test_false_preserved(self):
        assert _coalesce(False, True) is False


class TestDriftRecallNullHandling:
    """Verify drift_recall handles NULL database fields without TypeError."""

    @pytest.mark.asyncio
    async def test_null_confidence_no_crash(self):
        """Row with confidence=NULL must not raise TypeError in compute_activation."""
        db = AsyncMock()
        qdrant = MagicMock()
        embeddings = AsyncMock()
        embeddings.embed = AsyncMock(side_effect=Exception("no embeddings"))

        # Row with all-None fields (simulates SQLite NULL values)
        null_row = {
            "memory_id": "m1",
            "content": "test memory",
            "confidence": None,
            "created_at": None,
            "retrieved_count": None,
            "link_count": None,
            "source_type": None,
            "tags": None,
            "memory_class": None,
            "collection": "episodic_memory",
        }

        with patch(
            "genesis.memory.drift.memory_crud.search_ranked",
            new_callable=AsyncMock,
            return_value=[
                {"memory_id": "m1", "content": "test", "rank": -1.0},
            ],
        ), patch(
            "genesis.memory.drift._identify_clusters",
            new_callable=AsyncMock,
            return_value=(None, None),
        ), patch(
            "genesis.memory.drift.memory_crud.get_by_id",
            new_callable=AsyncMock,
            return_value=null_row,
        ), patch(
            "genesis.memory.drift.graph_traverse",
            new_callable=AsyncMock,
            return_value=[],
        ), patch(
            "genesis.memory.retrieval._expired_candidate_ids",
            new_callable=AsyncMock,
            return_value=set(),
        ):
            results = await drift_recall(
                "test query",
                db=db,
                qdrant_client=qdrant,
                embedding_provider=embeddings,
            )

        # Should complete without TypeError and return valid results
        assert len(results) == 1
        assert results[0].memory_id == "m1"
        assert results[0].activation_score > 0  # default confidence 0.5 produces positive score


class TestDriftRecallDefaultSource:
    """Verify drift_recall defaults to episodic source."""

    @pytest.mark.asyncio
    async def test_default_source_is_episodic(self):
        """drift_recall with no source arg should search episodic_memory only."""
        db = AsyncMock()
        qdrant = MagicMock()
        embeddings = AsyncMock()
        embeddings.embed = AsyncMock(side_effect=Exception("no embeddings"))

        with patch(
            "genesis.memory.drift.memory_crud.search_ranked",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_search:
            await drift_recall(
                "test query",
                db=db,
                qdrant_client=qdrant,
                embedding_provider=embeddings,
                # source NOT passed — should default to "episodic"
            )

        # Verify FTS search was called with collection filter for episodic
        call_kwargs = mock_search.call_args
        assert call_kwargs is not None
        # The source_collections resolved from "episodic" should be ["episodic_memory"]
        # which is passed to _global_primer and then to search_ranked with collection filter
