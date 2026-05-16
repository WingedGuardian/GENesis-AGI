"""Tests for dream cycle consolidation engine."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.dream_cycle import (
    MAX_BUCKET_SIZE,
    MAX_CLUSTER_SIZE,
    _build_synthesis_prompt,
    _parse_synthesis_response,
    _read_mem_available_mb,
    _size_distribution,
    _UnionFind,
    run,
)

# All Qdrant functions are imported inside function bodies, so we patch
# at the source (genesis.qdrant.collections.*) not at dream_cycle.*.
_SCROLL = "genesis.qdrant.collections.scroll_points"
_SEARCH = "genesis.qdrant.collections.search"
_UPDATE = "genesis.qdrant.collections.update_payload"
_DELETE = "genesis.qdrant.collections.delete_point"
_BATCH_VEC = "genesis.memory.dream_cycle._batch_get_vectors"


# ── Union-Find ───────────────────────────────────────────────────────────


class TestUnionFind:
    def test_singleton(self):
        uf = _UnionFind()
        assert uf.find("a") == "a"

    def test_union_two(self):
        uf = _UnionFind()
        uf.union("a", "b")
        assert uf.find("a") == uf.find("b")

    def test_union_chain(self):
        uf = _UnionFind()
        uf.union("a", "b")
        uf.union("b", "c")
        assert uf.find("a") == uf.find("c")

    def test_components(self):
        uf = _UnionFind()
        uf.union("a", "b")
        uf.union("c", "d")
        comps = uf.components()
        roots = list(comps.keys())
        assert len(roots) == 2
        sizes = sorted(len(v) for v in comps.values())
        assert sizes == [2, 2]

    def test_no_false_merge(self):
        uf = _UnionFind()
        uf.find("a")
        uf.find("b")
        assert uf.find("a") != uf.find("b")


# ── Prompt Building ──────────────────────────────────────────────────────


class TestBuildSynthesisPrompt:
    def test_includes_all_memories(self):
        cluster = [
            {"id": "m1", "payload": {"content": "fact A", "confidence": 0.9, "source": "s1", "created_at": "2026-01-01"}},
            {"id": "m2", "payload": {"content": "fact B", "confidence": 0.7, "source": "s2", "created_at": "2026-01-02"}},
        ]
        prompt = _build_synthesis_prompt(cluster, "memory", "retrieval")
        assert "fact A" in prompt
        assert "fact B" in prompt
        assert "wing=memory" in prompt
        assert "room=retrieval" in prompt
        assert "2 total" in prompt

    def test_handles_missing_fields(self):
        cluster = [
            {"id": "m1", "payload": {"content": "x"}},
            {"id": "m2", "payload": {"content": "y"}},
        ]
        prompt = _build_synthesis_prompt(cluster, "general", "uncategorized")
        assert "x" in prompt
        assert "y" in prompt


# ── Response Parsing ─────────────────────────────────────────────────────


class TestParseSynthesisResponse:
    def test_valid_json(self):
        response = json.dumps({
            "content": "merged content",
            "tags": ["a", "b"],
            "confidence": 0.95,
            "memory_class": "fact",
            "wing": "memory",
            "room": "store",
            "synthesis_notes": "combined A and B",
        })
        result = _parse_synthesis_response(response, "default_wing", "default_room")
        assert result["content"] == "merged content"
        assert result["confidence"] == 0.95
        assert result["wing"] == "memory"

    def test_json_with_markdown_fences(self):
        response = '```json\n{"content": "test", "tags": []}\n```'
        result = _parse_synthesis_response(response, "w", "r")
        assert result["content"] == "test"

    def test_invalid_json_fallback(self):
        response = "This is not JSON at all"
        result = _parse_synthesis_response(response, "w", "r")
        assert result["content"] == response
        assert result["wing"] == "w"
        assert result["room"] == "r"

    def test_missing_content_field(self):
        response = json.dumps({"tags": ["a"]})
        result = _parse_synthesis_response(response, "w", "r")
        # Falls back to raw response
        assert "tags" in result["content"]


# ── Size Distribution ────────────────────────────────────────────────────


class TestSizeDistribution:
    def test_categorizes_correctly(self):
        clusters = [
            [{"id": "1"}, {"id": "2"}],
            [{"id": "1"}, {"id": "2"}, {"id": "3"}],
            [{"id": str(i)} for i in range(5)],
            [{"id": str(i)} for i in range(8)],
            [{"id": str(i)} for i in range(15)],
        ]
        dist = _size_distribution(clusters)
        assert dist["2-3"] == 2
        assert dist["4-5"] == 1
        assert dist["6-10"] == 1
        assert dist["11+"] == 1


# ── Dry-Run Integration ─────────────────────────────────────────────────


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_returns_report_without_changes(self):
        """Dry run computes clusters but doesn't write anything."""
        mock_qdrant = MagicMock()
        mock_db = AsyncMock()
        mock_router = AsyncMock()
        mock_store = AsyncMock()

        with patch(_SCROLL) as mock_scroll, \
             patch(_SEARCH) as mock_search, \
             patch(_BATCH_VEC) as mock_vec:
            mock_scroll.return_value = (
                [
                    {"id": "p1", "payload": {"wing": "memory", "room": "store", "content": "fact A", "confidence": 0.9}},
                    {"id": "p2", "payload": {"wing": "memory", "room": "store", "content": "fact A rephrased", "confidence": 0.8}},
                ],
                None,
            )
            mock_search.return_value = [
                {"id": "p2", "score": 0.92, "payload": {}},
            ]
            mock_vec.return_value = {"p1": [0.1] * 768, "p2": [0.1] * 768}

            report = await run(
                qdrant=mock_qdrant,
                db=mock_db,
                router=mock_router,
                store=mock_store,
                dry_run=True,
            )

        assert report["dry_run"] is True
        assert report["clusters_found"] >= 1
        assert report["clusters_merged"] == 0
        mock_store.store.assert_not_called()
        mock_db.execute.assert_not_called()


class TestLiveRun:
    @pytest.mark.asyncio
    async def test_live_run_synthesizes_and_deprecates(self):
        """Live run calls LLM, stores synthesis, deprecates originals."""
        mock_qdrant = MagicMock()
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_router = AsyncMock()
        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="new-synth-id")
        mock_store.linker = None

        synthesis_json = json.dumps({
            "content": "Merged fact A+B",
            "tags": ["test"],
            "confidence": 0.9,
            "memory_class": "fact",
            "wing": "memory",
            "room": "store",
            "synthesis_notes": "combined",
        })
        mock_router.route_call = AsyncMock(
            return_value=MagicMock(success=True, content=synthesis_json),
        )

        with patch(_SCROLL) as mock_scroll, \
             patch(_SEARCH) as mock_search, \
             patch(_BATCH_VEC) as mock_vec, \
             patch(_UPDATE) as mock_update:
            mock_scroll.return_value = (
                [
                    {"id": "p1", "payload": {"wing": "memory", "room": "store", "content": "fact A", "confidence": 0.9}},
                    {"id": "p2", "payload": {"wing": "memory", "room": "store", "content": "fact A v2", "confidence": 0.8}},
                ],
                None,
            )
            mock_search.return_value = [
                {"id": "p2", "score": 0.92, "payload": {}},
            ]
            mock_vec.return_value = {"p1": [0.1] * 768, "p2": [0.1] * 768}

            report = await run(
                qdrant=mock_qdrant,
                db=mock_db,
                router=mock_router,
                store=mock_store,
                dry_run=False,
            )

        assert report["dry_run"] is False
        assert report["clusters_merged"] >= 1
        assert report["memories_deprecated"] >= 2
        mock_store.store.assert_called_once()
        assert mock_store.store.call_args[1]["source_pipeline"] == "dream_cycle"
        # 1 for synthesized_from + 2 for deprecated originals
        assert mock_update.call_count >= 3

    @pytest.mark.asyncio
    async def test_skips_large_clusters(self):
        """Clusters > MAX_CLUSTER_SIZE are skipped."""
        mock_qdrant = MagicMock()
        mock_db = AsyncMock()
        mock_router = AsyncMock()
        mock_store = AsyncMock()
        mock_store.linker = None

        large_cluster_points = [
            {"id": f"p{i}", "payload": {"wing": "memory", "room": "store", "content": f"fact {i}", "confidence": 0.5}}
            for i in range(MAX_CLUSTER_SIZE + 5)
        ]

        with patch(_SCROLL) as mock_scroll, \
             patch(_SEARCH) as mock_search, \
             patch(_BATCH_VEC) as mock_vec:
            mock_scroll.return_value = (large_cluster_points, None)
            mock_search.side_effect = lambda *a, **kw: [
                {"id": p["id"], "score": 0.95, "payload": {}}
                for p in large_cluster_points
            ]
            mock_vec.return_value = {
                p["id"]: [0.1] * 768 for p in large_cluster_points
            }

            report = await run(
                qdrant=mock_qdrant,
                db=mock_db,
                router=mock_router,
                store=mock_store,
                dry_run=False,
            )

        assert report["clusters_skipped_large"] >= 1
        assert report["clusters_merged"] == 0
        mock_store.store.assert_not_called()


# ── Rollback ─────────────────────────────────────────────────────────────


class TestRollback:
    @pytest.mark.asyncio
    async def test_rollback_restores_and_deletes(self):
        """Rollback restores deprecated originals and deletes syntheses."""
        from genesis.memory.dream_cycle import rollback

        mock_qdrant = MagicMock()
        mock_db = AsyncMock()

        deprecated_cursor = AsyncMock()
        deprecated_cursor.fetchall = AsyncMock(return_value=[("orig-1",), ("orig-2",)])

        synthesis_cursor = AsyncMock()
        synthesis_cursor.fetchall = AsyncMock(return_value=[("synth-1",)])

        mock_db.execute = AsyncMock(side_effect=[
            deprecated_cursor,   # SELECT deprecated
            AsyncMock(),         # UPDATE orig-1
            AsyncMock(),         # UPDATE orig-2
            synthesis_cursor,    # SELECT synthesis
            AsyncMock(),         # DELETE metadata
            AsyncMock(),         # DELETE fts
            AsyncMock(),         # DELETE links
        ])
        mock_db.commit = AsyncMock()

        with patch(_UPDATE), patch(_DELETE):
            report = await rollback(
                "test-run-id",
                qdrant=mock_qdrant,
                db=mock_db,
            )

        assert report["restored"] == 2
        assert report["syntheses_deleted"] == 1
        assert len(report["errors"]) == 0


# ── Bucket Chunking ────────────────────────────────────────────────────


class TestBucketChunking:
    @pytest.mark.asyncio
    async def test_large_bucket_is_chunked(self):
        """A bucket exceeding MAX_BUCKET_SIZE gets split into chunks."""
        mock_qdrant = MagicMock()
        mock_db = AsyncMock()
        mock_router = AsyncMock()
        mock_store = AsyncMock()

        # Create a bucket larger than MAX_BUCKET_SIZE
        n_points = MAX_BUCKET_SIZE + 200  # 700 points → 2 chunks
        points = [
            {
                "id": f"p{i}",
                "payload": {
                    "wing": "memory", "room": "store",
                    "content": f"fact {i}", "confidence": 0.5,
                },
            }
            for i in range(n_points)
        ]

        search_call_count = 0

        def mock_search_fn(*args, **kwargs):
            nonlocal search_call_count
            search_call_count += 1
            # Return no neighbors — no clusters formed
            return []

        with patch(_SCROLL) as mock_scroll, \
             patch(_SEARCH, side_effect=mock_search_fn), \
             patch(_BATCH_VEC) as mock_vec:
            mock_scroll.return_value = (points, None)
            mock_vec.return_value = {
                f"p{i}": [0.1] * 768 for i in range(n_points)
            }

            report = await run(
                qdrant=mock_qdrant,
                db=mock_db,
                router=mock_router,
                store=mock_store,
                dry_run=True,
            )

        # All points should be searched (across chunks)
        assert search_call_count == n_points
        assert report["total_points"] == n_points
        # bucket_sizes should report the full pre-chunk size
        assert report["bucket_sizes"]["memory/store"] == n_points

    @pytest.mark.asyncio
    async def test_tail_chunk_of_one_skipped(self):
        """A bucket of MAX_BUCKET_SIZE+1 splits into 500 + 1; the 1-point tail is skipped."""
        mock_qdrant = MagicMock()
        mock_db = AsyncMock()
        mock_router = AsyncMock()
        mock_store = AsyncMock()

        n_points = MAX_BUCKET_SIZE + 1  # 501 → chunks of 500 + 1
        points = [
            {
                "id": f"p{i}",
                "payload": {
                    "wing": "memory", "room": "store",
                    "content": f"fact {i}", "confidence": 0.5,
                },
            }
            for i in range(n_points)
        ]

        search_call_count = 0

        def mock_search_fn(*args, **kwargs):
            nonlocal search_call_count
            search_call_count += 1
            return []

        with patch(_SCROLL) as mock_scroll, \
             patch(_SEARCH, side_effect=mock_search_fn), \
             patch(_BATCH_VEC) as mock_vec:
            mock_scroll.return_value = (points, None)
            mock_vec.return_value = {
                f"p{i}": [0.1] * 768 for i in range(n_points)
            }

            report = await run(
                qdrant=mock_qdrant,
                db=mock_db,
                router=mock_router,
                store=mock_store,
                dry_run=True,
            )

        # Only 500 points searched — tail chunk of 1 skipped
        assert search_call_count == MAX_BUCKET_SIZE

    @pytest.mark.asyncio
    async def test_small_bucket_not_chunked(self):
        """Buckets within MAX_BUCKET_SIZE are processed in one pass."""
        mock_qdrant = MagicMock()
        mock_db = AsyncMock()
        mock_router = AsyncMock()
        mock_store = AsyncMock()

        points = [
            {
                "id": f"p{i}",
                "payload": {
                    "wing": "memory", "room": "store",
                    "content": f"fact {i}", "confidence": 0.5,
                },
            }
            for i in range(10)
        ]

        with patch(_SCROLL) as mock_scroll, \
             patch(_SEARCH, return_value=[]), \
             patch(_BATCH_VEC) as mock_vec:
            mock_scroll.return_value = (points, None)
            mock_vec.return_value = {
                f"p{i}": [0.1] * 768 for i in range(10)
            }

            report = await run(
                qdrant=mock_qdrant,
                db=mock_db,
                router=mock_router,
                store=mock_store,
                dry_run=True,
            )

        assert report["total_points"] == 10
        assert report["bucket_sizes"]["memory/store"] == 10


# ── Memory Preflight ────────────────────────────────────────────────────


class TestMemoryPreflight:
    @pytest.mark.asyncio
    async def test_aborts_on_low_memory(self):
        """Dream cycle aborts early when available memory is too low."""
        mock_qdrant = MagicMock()
        mock_db = AsyncMock()
        mock_router = AsyncMock()
        mock_store = AsyncMock()

        with patch(
            "genesis.memory.dream_cycle._read_mem_available_mb",
            return_value=100,  # 100MB < 256MB threshold
        ):
            report = await run(
                qdrant=mock_qdrant,
                db=mock_db,
                router=mock_router,
                store=mock_store,
                dry_run=True,
            )

        assert "aborted" in report
        assert "low_memory" in report["aborted"]
        # Should NOT have scrolled Qdrant at all
        assert "total_points" not in report

    @pytest.mark.asyncio
    async def test_proceeds_when_memory_ok(self):
        """Dream cycle proceeds when sufficient memory is available."""
        mock_qdrant = MagicMock()
        mock_db = AsyncMock()
        mock_router = AsyncMock()
        mock_store = AsyncMock()

        with patch(
            "genesis.memory.dream_cycle._read_mem_available_mb",
            return_value=8000,  # 8GB — plenty
        ), patch(_SCROLL, return_value=([], None)):
            report = await run(
                qdrant=mock_qdrant,
                db=mock_db,
                router=mock_router,
                store=mock_store,
                dry_run=True,
            )

        assert "aborted" not in report
        assert report["total_points"] == 0

    @pytest.mark.asyncio
    async def test_skips_check_on_non_linux(self):
        """Non-Linux systems (None return) skip preflight, proceed normally."""
        mock_qdrant = MagicMock()
        mock_db = AsyncMock()
        mock_router = AsyncMock()
        mock_store = AsyncMock()

        with patch(
            "genesis.memory.dream_cycle._read_mem_available_mb",
            return_value=None,  # Non-Linux
        ), patch(_SCROLL, return_value=([], None)):
            report = await run(
                qdrant=mock_qdrant,
                db=mock_db,
                router=mock_router,
                store=mock_store,
                dry_run=True,
            )

        assert "aborted" not in report

    def test_read_mem_available_returns_int(self):
        """Smoke test — on Linux this should return an int."""
        result = _read_mem_available_mb()
        # We're on Linux, so this should work
        assert result is not None
        assert isinstance(result, int)
        assert result > 0


# ── Async Yielding ──────────────────────────────────────────────────────


class TestAsyncYielding:
    @pytest.mark.asyncio
    async def test_yields_during_search_loop(self):
        """asyncio.sleep(0) is called periodically during clustering."""
        mock_qdrant = MagicMock()
        mock_db = AsyncMock()
        mock_router = AsyncMock()
        mock_store = AsyncMock()

        # Create exactly 100 points to trigger 2 yields (at 50 and 100)
        n_points = 100
        points = [
            {
                "id": f"p{i}",
                "payload": {
                    "wing": "memory", "room": "store",
                    "content": f"fact {i}", "confidence": 0.5,
                },
            }
            for i in range(n_points)
        ]

        with patch(_SCROLL) as mock_scroll, \
             patch(_SEARCH, return_value=[]), \
             patch(_BATCH_VEC) as mock_vec, \
             patch("genesis.memory.dream_cycle.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_scroll.return_value = (points, None)
            mock_vec.return_value = {
                f"p{i}": [0.1] * 768 for i in range(n_points)
            }

            await run(
                qdrant=mock_qdrant,
                db=mock_db,
                router=mock_router,
                store=mock_store,
                dry_run=True,
            )

        # With 100 points and _YIELD_EVERY=50, sleep(0) called twice
        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(0)
