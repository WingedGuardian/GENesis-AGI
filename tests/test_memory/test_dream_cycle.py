"""Tests for dream cycle consolidation engine."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.dream_cycle import (
    MAX_CLUSTER_SIZE,
    _UnionFind,
    _build_synthesis_prompt,
    _parse_synthesis_response,
    _size_distribution,
    run,
)

# All Qdrant functions are imported inside function bodies, so we patch
# at the source (genesis.qdrant.collections.*) not at dream_cycle.*.
_SCROLL = "genesis.qdrant.collections.scroll_points"
_SEARCH = "genesis.qdrant.collections.search"
_UPDATE = "genesis.qdrant.collections.update_payload"
_DELETE = "genesis.qdrant.collections.delete_point"
_GET_VEC = "genesis.memory.dream_cycle._get_vector"


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
             patch(_GET_VEC) as mock_vec:
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
            mock_vec.return_value = [0.1] * 768

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
             patch(_GET_VEC) as mock_vec, \
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
            mock_vec.return_value = [0.1] * 768

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
             patch(_GET_VEC) as mock_vec:
            mock_scroll.return_value = (large_cluster_points, None)
            mock_search.side_effect = lambda *a, **kw: [
                {"id": p["id"], "score": 0.95, "payload": {}}
                for p in large_cluster_points
            ]
            mock_vec.return_value = [0.1] * 768

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
