"""Tests for genesis.memory.drift — DRIFT multi-mode retrieval."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.drift import (
    _coalesce,
    _global_primer,
    _identify_clusters,
    _local_drilldown,
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
            "genesis.memory.drift._global_primer",
            new_callable=AsyncMock,
            return_value=([], None, None),
        ) as mock_primer:
            await drift_recall(
                "test query",
                db=db,
                qdrant_client=qdrant,
                embedding_provider=embeddings,
                # source NOT passed — should default to "episodic"
            )

        # Verify _global_primer was called with episodic-only collections
        mock_primer.assert_called_once()
        call_kwargs = mock_primer.call_args.kwargs
        assert call_kwargs["source_collections"] == ["episodic_memory"], (
            f"Expected ['episodic_memory'], got {call_kwargs['source_collections']!r}"
        )


class TestLocalDrilldownCollections:
    """MEM-006: the scoped FTS drill-down must honor source_collections.

    The vector arm already loops over ``source_collections``; the FTS arm
    hardcoded ``episodic_memory``, making knowledge recall vector-only in
    the wing-scoped phase.
    """

    @staticmethod
    def _failing_embeddings():
        embeddings = AsyncMock()
        embeddings.embed = AsyncMock(side_effect=Exception("no embeddings"))
        return embeddings

    @pytest.mark.asyncio
    async def test_fts_drilldown_searches_all_source_collections(self):
        """With source='both'-style collections, FTS must hit each one."""
        # Both ids carry the requested wing (projected by search_ranked)
        db = MagicMock()
        seen_collections: list[str | None] = []

        async def fake_search_ranked(db_, *, query, limit,
                                     collection=None, **kw):
            seen_collections.append(collection)
            if collection == "knowledge_base":
                return [{"memory_id": "kb1", "content": "kb", "rank": -1.0,
                         "wing": "memory"}]
            return [{"memory_id": "ep1", "content": "ep", "rank": -2.0,
                     "wing": "memory"}]

        with patch(
            "genesis.memory.drift.memory_crud.search_ranked",
            new=AsyncMock(side_effect=fake_search_ranked),
        ):
            ids = await _local_drilldown(
                "test query",
                db=db,
                qdrant_client=MagicMock(),
                embedding_provider=self._failing_embeddings(),
                source_collections=["episodic_memory", "knowledge_base"],
                wing="memory",
                room=None,
                global_ids=[],
            )

        assert seen_collections == ["episodic_memory", "knowledge_base"], (
            f"FTS drill-down searched {seen_collections!r}, expected one "
            "search per source collection"
        )
        assert "kb1" in ids, "knowledge_base FTS hit must survive the drill-down"
        assert "ep1" in ids

    @pytest.mark.asyncio
    async def test_fts_drilldown_episodic_only_unchanged(self):
        """Default episodic source still searches exactly one collection."""
        db = MagicMock()
        seen_collections: list[str | None] = []

        async def fake_search_ranked(db_, *, query, limit,
                                     collection=None, **kw):
            seen_collections.append(collection)
            return [{"memory_id": "ep1", "content": "ep", "rank": -2.0,
                     "wing": "memory"}]

        with patch(
            "genesis.memory.drift.memory_crud.search_ranked",
            new=AsyncMock(side_effect=fake_search_ranked),
        ):
            ids = await _local_drilldown(
                "test query",
                db=db,
                qdrant_client=MagicMock(),
                embedding_provider=self._failing_embeddings(),
                source_collections=["episodic_memory"],
                wing="memory",
                room=None,
                global_ids=[],
            )

        assert seen_collections == ["episodic_memory"]
        assert ids == ["ep1"]

    @pytest.mark.asyncio
    async def test_fts_drilldown_merges_by_rank_across_collections(self):
        """A knowledge hit with a BETTER FTS rank must precede weaker
        episodic hits in the returned order (Codex P2): local_ids feeds RRF
        as a ranked list, so naive per-collection appending would rank every
        hit of the first collection above the second's regardless of
        relevance. Ranks are comparable — same memory_fts table, same query.
        """
        # Wing filter passes all three (wing projected by search_ranked) — the
        # filter must also PRESERVE the rank-merged order, not rescramble it
        db = MagicMock()
        rows_by_collection = {
            "episodic_memory": [
                {"memory_id": "ep1", "content": "e1", "rank": -5.0,
                 "wing": "memory"},
                {"memory_id": "ep2", "content": "e2", "rank": -3.0,
                 "wing": "memory"},
            ],
            # kb1 outranks both episodic hits (more negative = better)
            "knowledge_base": [
                {"memory_id": "kb1", "content": "k1", "rank": -9.0,
                 "wing": "memory"},
            ],
        }

        async def fake_search_ranked(db_, *, query, limit,
                                     collection=None, **kw):
            return rows_by_collection[collection]

        with patch(
            "genesis.memory.drift.memory_crud.search_ranked",
            new=AsyncMock(side_effect=fake_search_ranked),
        ):
            ids = await _local_drilldown(
                "test query",
                db=db,
                qdrant_client=MagicMock(),
                embedding_provider=self._failing_embeddings(),
                source_collections=["episodic_memory", "knowledge_base"],
                wing="memory",
                room=None,
                global_ids=[],
            )

        assert ids == ["kb1", "ep1", "ep2"], (
            f"expected rank-merged order [kb1, ep1, ep2], got {ids!r}"
        )

    @pytest.mark.asyncio
    async def test_fts_drilldown_filters_nonmatching_wing(self):
        """Rows whose authoritative wing (projected by search_ranked) differs
        from the requested wing are dropped; NULL wing is dropped too."""
        db = MagicMock()

        async def fake_search_ranked(db_, *, query, limit,
                                     collection=None, **kw):
            return [
                {"memory_id": "keep", "content": "k", "rank": -5.0,
                 "wing": "memory"},
                {"memory_id": "drop", "content": "d", "rank": -4.0,
                 "wing": "routing"},
                {"memory_id": "null", "content": "n", "rank": -3.0,
                 "wing": None},
            ]

        with patch(
            "genesis.memory.drift.memory_crud.search_ranked",
            new=AsyncMock(side_effect=fake_search_ranked),
        ):
            ids = await _local_drilldown(
                "test query",
                db=db,
                qdrant_client=MagicMock(),
                embedding_provider=self._failing_embeddings(),
                source_collections=["episodic_memory"],
                wing="memory",
                room=None,
                global_ids=[],
            )

        assert ids == ["keep"], (
            f"only the wing='memory' row should survive, got {ids!r}"
        )
