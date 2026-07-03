"""Tests for dream cycle consolidation engine."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.dream_cycle import (
    _CAPACITY_ABORT_THRESHOLD,
    MAX_BUCKET_SIZE,
    MAX_CLUSTER_SIZE,
    WORKLIST_WORK_TYPE,
    _build_synthesis_prompt,
    _CapacityBreaker,
    _parse_synthesis_response,
    _persist_worklist,
    _rank_and_cap_clusters,
    _read_mem_available_mb,
    _size_distribution,
    _synthesize_clusters,
    _UnionFind,
    run,
    run_synthesis_drain,
)
from genesis.resilience.deferred_work import DeferredWorkQueue


def _fake_cluster(n: int = 2, wing: str = "memory", room: str = "store"):
    """A minimal cluster of n memory points for synthesis-loop tests."""
    return [
        {"id": f"{wing}-{room}-{i}", "wing": wing, "room": room,
         "payload": {"content": f"fact {i}", "confidence": 0.8}}
        for i in range(n)
    ]


def _fresh_report():
    return {
        "clusters_merged": 0, "clusters_skipped_large": 0,
        "memories_deprecated": 0, "adversarial_blocked": 0,
        "shrink_gate_blocked": 0, "rollback_flagged": False,
        "aborted_capacity": False, "errors": [],
    }

# All Qdrant functions are imported inside function bodies, so we patch
# at the source (genesis.qdrant.collections.*) not at dream_cycle.*.
_SCROLL = "genesis.qdrant.collections.scroll_points"
_SEARCH = "genesis.qdrant.collections.search"
_UPDATE = "genesis.qdrant.collections.update_payload"
_DELETE = "genesis.qdrant.collections.delete_point"
_BATCH_VEC = "genesis.memory.dream_cycle._batch_get_vectors"


# ── Capacity breaker ─────────────────────────────────────────────────────


class TestCapacityBreaker:
    def test_trips_after_threshold_consecutive_exhaustions(self):
        b = _CapacityBreaker(threshold=3)
        assert b.tripped is False
        b.record_exhaustion()
        b.record_exhaustion()
        assert b.tripped is False
        b.record_exhaustion()
        assert b.tripped is True

    def test_progress_resets_the_streak(self):
        b = _CapacityBreaker(threshold=3)
        b.record_exhaustion()
        b.record_exhaustion()
        b.record_progress()
        b.record_exhaustion()
        b.record_exhaustion()
        assert b.tripped is False


class TestSynthesizeClustersBreaker:
    @pytest.mark.asyncio
    async def test_aborts_on_consecutive_exhaustion(self):
        """Provider-chain exhaustion aborts the loop after the threshold instead
        of grinding every cluster (the 2026-06-14 pathology)."""
        clusters = [_fake_cluster() for _ in range(_CAPACITY_ABORT_THRESHOLD + 3)]
        router = AsyncMock()
        router.route_call = AsyncMock(return_value=MagicMock(
            success=False, error="All providers exhausted",
        ))
        report = _fresh_report()
        await _synthesize_clusters(
            clusters, run_id="t", qdrant=MagicMock(), db=AsyncMock(),
            router=router, store=AsyncMock(),
            max_merges=100, max_cluster_size=10, report=report,
        )
        assert report["aborted_capacity"] is True
        assert report["clusters_merged"] == 0
        # Aborted after ~threshold attempts, NOT all clusters
        assert len(report["errors"]) == _CAPACITY_ABORT_THRESHOLD
        # rollback flag must NOT fire on a capacity abort
        assert report["rollback_flagged"] is False

    @pytest.mark.asyncio
    async def test_quality_blocks_do_not_abort(self):
        """Genuine adversarial quality blocks do NOT trip the capacity breaker —
        every cluster is processed."""
        n = _CAPACITY_ABORT_THRESHOLD + 3
        clusters = [_fake_cluster() for _ in range(n)]
        synthesis_json = json.dumps({
            "content": "consolidated " * 20, "tags": ["t"], "confidence": 0.9,
            "memory_class": "fact", "wing": "memory", "room": "store",
            "synthesis_notes": "x",
        })
        challenge_fail = json.dumps({"verdict": "FAIL", "missing": ["a detail"]})

        async def _call(call_site, messages, **kw):
            if call_site == "dream_cycle_synthesis":
                return MagicMock(success=True, content=synthesis_json)
            return MagicMock(success=True, content=challenge_fail)
        router = AsyncMock()
        router.route_call = AsyncMock(side_effect=_call)
        report = _fresh_report()
        await _synthesize_clusters(
            clusters, run_id="t", qdrant=MagicMock(), db=AsyncMock(),
            router=router, store=AsyncMock(),
            max_merges=100, max_cluster_size=10, report=report,
        )
        assert report["aborted_capacity"] is False
        assert report["adversarial_blocked"] == n
        assert report["clusters_merged"] == 0


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
        """Dry run clusters + persists the worklist but stores no memories."""
        mock_qdrant = MagicMock()
        mock_db = AsyncMock()
        mock_router = AsyncMock()
        mock_store = AsyncMock()

        with patch(_SCROLL) as mock_scroll, \
             patch(_SEARCH) as mock_search, \
             patch(_BATCH_VEC) as mock_vec, \
             patch(
                 "genesis.memory.dream_cycle._persist_worklist",
                 new_callable=AsyncMock,
                 return_value={"enqueued": 1, "oversize_flagged": 0, "superseded": 0},
             ) as mock_persist:
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
        # Worklist maintenance runs in ALL modes — clustering + enqueue IS the
        # weekly job; the merge decision is gated on the drain side.
        mock_persist.assert_awaited_once()
        assert report["worklist_enqueued"] == 1
        # Synthesis-outcome keys moved to the drain report — a permanent
        # "0 merged" here must not masquerade as a synthesis result.
        assert "clusters_merged" not in report
        # Consolidation must not write any new memories in dry_run
        mock_store.store.assert_not_called()
        # Note: Sprint 2 phases (link repair, entity resolution, etc.) may
        # issue read-only DB queries even in dry_run — that's expected.
        # The key assertion is that no synthesized memories were stored.


class TestSynthesizeClustersSkipsLarge:
    @pytest.mark.asyncio
    async def test_oversize_cluster_skipped_no_llm(self):
        """Defense-in-depth: even if an oversize cluster reaches synthesis
        (worklist excludes them at enqueue), it is skipped without LLM calls."""
        router = AsyncMock()
        report = _fresh_report()
        await _synthesize_clusters(
            [_fake_cluster(MAX_CLUSTER_SIZE + 2)],
            run_id="t", qdrant=MagicMock(), db=AsyncMock(),
            router=router, store=AsyncMock(),
            max_merges=100, max_cluster_size=MAX_CLUSTER_SIZE, report=report,
        )
        assert report["clusters_skipped_large"] == 1
        assert report["clusters_merged"] == 0
        router.route_call.assert_not_called()


# ── Worklist persistence (weekly → queue) ───────────────────────────────


def _clusters_of_sizes(*sizes: int):
    return [_fake_cluster(n, room=f"r{i}") for i, n in enumerate(sizes)]


class TestWorklist:
    @pytest.mark.asyncio
    async def test_enqueues_value_ordered_and_supersedes(self, db):
        """Slices drain biggest-first; a fresh weekly run replaces the old list."""
        queue = DeferredWorkQueue(db)
        result = await _persist_worklist(
            db, _clusters_of_sizes(2, 5, 3), weekly_run_id="week-1",
        )
        assert result == {"enqueued": 3, "oversize_flagged": 0, "superseded": 0}
        assert await queue.count_pending(work_type=WORKLIST_WORK_TYPE) == 3

        # FIFO-within-priority == enqueue order == size desc
        first = await queue.next_pending(work_type=WORKLIST_WORK_TYPE)
        payload = json.loads(first["payload_json"])
        assert len(payload["member_ids"]) == 5
        assert payload["weekly_run_id"] == "week-1"
        assert payload["value_score"] == 5

        # A fresh weekly re-cluster is authoritative: old rows deleted
        result2 = await _persist_worklist(
            db, _clusters_of_sizes(4), weekly_run_id="week-2",
        )
        assert result2["superseded"] == 3
        assert await queue.count_pending(work_type=WORKLIST_WORK_TYPE) == 1
        only = await queue.next_pending(work_type=WORKLIST_WORK_TYPE)
        assert json.loads(only["payload_json"])["weekly_run_id"] == "week-2"

    @pytest.mark.asyncio
    async def test_top_k_cap(self, db):
        """Only the top-K by value are persisted; the tail is dropped."""
        queue = DeferredWorkQueue(db)
        result = await _persist_worklist(
            db, _clusters_of_sizes(2, 6, 3, 5, 4), weekly_run_id="w", cap=3,
        )
        assert result["enqueued"] == 3
        sizes = []
        for _ in range(3):
            item = await queue.next_pending(work_type=WORKLIST_WORK_TYPE)
            sizes.append(json.loads(item["payload_json"])["value_score"])
            await queue.mark_completed(item["id"])
        assert sizes == [6, 5, 4]

    @pytest.mark.asyncio
    async def test_oversize_excluded_and_flagged(self, db):
        """Clusters > MAX_CLUSTER_SIZE never enter the worklist (FM-1) — they
        would rank FIRST (size desc) yet be unmergeable, wasting drain budget
        weekly and making shadow reports unfaithful to live behavior."""
        queue = DeferredWorkQueue(db)
        clusters = [_fake_cluster(MAX_CLUSTER_SIZE + 3), _fake_cluster(3)]
        result = await _persist_worklist(db, clusters, weekly_run_id="w")
        assert result["enqueued"] == 1
        assert result["oversize_flagged"] == 1
        item = await queue.next_pending(work_type=WORKLIST_WORK_TYPE)
        assert len(json.loads(item["payload_json"])["member_ids"]) == 3

    def test_rank_and_cap_excludes_oversize(self):
        ranked = _rank_and_cap_clusters(
            _clusters_of_sizes(12, 4, 2), cap=10, max_cluster_size=10,
        )
        assert [len(c) for c in ranked] == [4, 2]


# ── Daily synthesis drain ────────────────────────────────────────────────


def _drain_qdrant(points_by_id: dict[str, dict]):
    """Qdrant mock for the drain: healthy preflight + batch retrieve that
    returns only the ids present in ``points_by_id`` (Qdrant semantics)."""
    q = MagicMock()

    def _retrieve(*, collection_name, ids, with_payload, with_vectors):
        return [
            SimpleNamespace(id=i, payload=points_by_id[i])
            for i in ids if i in points_by_id
        ]

    q.retrieve = MagicMock(side_effect=_retrieve)
    return q


def _live_points(cluster) -> dict[str, dict]:
    return {item["id"]: dict(item["payload"]) for item in cluster}


class TestSynthesisDrain:
    @pytest.mark.asyncio
    async def test_shadow_reports_would_merge_without_mutation(self, db):
        """SHADOW exercises the full queue lifecycle: no LLM, no writes."""
        clusters = _clusters_of_sizes(3, 2)
        await _persist_worklist(db, clusters, weekly_run_id="w")
        points = {**_live_points(clusters[0]), **_live_points(clusters[1])}
        router = AsyncMock()
        store = AsyncMock()

        report = await run_synthesis_drain(
            qdrant=_drain_qdrant(points), db=db, router=router, store=store,
            budget=10, dry_run=True,
        )

        assert report["dry_run"] is True
        assert report["drained"] == 2
        assert report["would_merge"] == 2
        assert report["clusters_merged"] == 0
        router.route_call.assert_not_called()
        store.store.assert_not_called()
        queue = DeferredWorkQueue(db)
        assert await queue.count_pending(work_type=WORKLIST_WORK_TYPE) == 0

    @pytest.mark.asyncio
    async def test_drain_honors_budget(self, db):
        """At most ``budget`` slices are consumed per drain."""
        clusters = _clusters_of_sizes(2, 2, 2, 2, 2)
        await _persist_worklist(db, clusters, weekly_run_id="w")
        points: dict[str, dict] = {}
        for c in clusters:
            points.update(_live_points(c))

        report = await run_synthesis_drain(
            qdrant=_drain_qdrant(points), db=db,
            router=AsyncMock(), store=AsyncMock(), budget=2, dry_run=True,
        )

        assert report["drained"] == 2
        queue = DeferredWorkQueue(db)
        assert await queue.count_pending(work_type=WORKLIST_WORK_TYPE) == 3

    @pytest.mark.asyncio
    async def test_stale_slice_completed_not_discarded(self, db):
        """<2 live members = normal lifecycle no-op: completed, NOT discarded —
        discarded rows surface as failures on the dashboard errors view (FM-5)."""
        cluster = _fake_cluster(2)
        await _persist_worklist(db, [cluster], weekly_run_id="w")
        points = _live_points(cluster)
        points[cluster[0]["id"]]["deprecated"] = True  # one member deprecated

        report = await run_synthesis_drain(
            qdrant=_drain_qdrant(points), db=db,
            router=AsyncMock(), store=AsyncMock(), budget=10, dry_run=True,
        )

        assert report["stale_skipped"] == 1
        assert report["would_merge"] == 0
        cursor = await db.execute(
            "SELECT status FROM deferred_work_queue WHERE work_type = ?",
            (WORKLIST_WORK_TYPE,),
        )
        rows = await cursor.fetchall()
        assert [r["status"] for r in rows] == ["completed"]

    @pytest.mark.asyncio
    async def test_qdrant_preflight_abort_consumes_nothing(self, db):
        """Qdrant down at drain start: abort without touching the worklist —
        an outage must not mass-discard the week's top-value slices (FM-2)."""
        await _persist_worklist(db, _clusters_of_sizes(2), weekly_run_id="w")
        qdrant = MagicMock()
        qdrant.get_collection = MagicMock(side_effect=ConnectionError("down"))

        report = await run_synthesis_drain(
            qdrant=qdrant, db=db,
            router=AsyncMock(), store=AsyncMock(), budget=10, dry_run=True,
        )

        assert report["aborted_infra"] is True
        assert report["drained"] == 0
        queue = DeferredWorkQueue(db)
        assert await queue.count_pending(work_type=WORKLIST_WORK_TYPE) == 1

    @pytest.mark.asyncio
    async def test_mid_drain_infra_error_resets_item(self, db):
        """Qdrant dies after preflight: in-flight item goes back to pending
        and the drain aborts — never discard on infrastructure failure (FM-2)."""
        await _persist_worklist(db, _clusters_of_sizes(2), weekly_run_id="w")
        qdrant = MagicMock()
        qdrant.retrieve = MagicMock(side_effect=ConnectionError("died mid-drain"))

        report = await run_synthesis_drain(
            qdrant=qdrant, db=db,
            router=AsyncMock(), store=AsyncMock(), budget=10, dry_run=True,
        )

        assert report["aborted_infra"] is True
        assert report["drained"] == 0
        queue = DeferredWorkQueue(db)
        assert await queue.count_pending(work_type=WORKLIST_WORK_TYPE) == 1

    @pytest.mark.asyncio
    async def test_superseded_item_skipped(self, db):
        """mark_processing returning False (row deleted by a racing weekly
        supersede) skips the item instead of processing a ghost (FM-10)."""
        cluster = _fake_cluster(2)
        await _persist_worklist(db, [cluster], weekly_run_id="w")

        with patch.object(
            DeferredWorkQueue, "mark_processing",
            new_callable=AsyncMock, return_value=False,
        ):
            report = await run_synthesis_drain(
                qdrant=_drain_qdrant(_live_points(cluster)), db=db,
                router=AsyncMock(), store=AsyncMock(), budget=3, dry_run=True,
            )

        assert report["drained"] == 0
        assert report["would_merge"] == 0

    @pytest.mark.asyncio
    async def test_live_drain_synthesizes_and_deprecates(self, db):
        """LIVE drain: rehydrates, calls LLM, stores synthesis, deprecates
        originals, and completes the queue item (the old weekly live path,
        now driven from the drain)."""
        cluster = _fake_cluster(2)
        await _persist_worklist(db, [cluster], weekly_run_id="w")

        store = AsyncMock()
        store.store = AsyncMock(return_value="new-synth-id")
        store.linker = None
        synthesis_json = json.dumps({
            "content": "Merged fact 0 and fact 1 into one canonical record",
            "tags": ["test"], "confidence": 0.9, "memory_class": "fact",
            "wing": "memory", "room": "store", "synthesis_notes": "combined",
        })
        adversarial_json = json.dumps({"verdict": "PASS"})
        router = AsyncMock()
        router.route_call = AsyncMock(side_effect=[
            MagicMock(success=True, content=synthesis_json),
            MagicMock(success=True, content=adversarial_json),
        ])

        with patch(_UPDATE) as mock_update:
            report = await run_synthesis_drain(
                qdrant=_drain_qdrant(_live_points(cluster)), db=db,
                router=router, store=store, budget=10, dry_run=False,
            )

        assert report["dry_run"] is False
        assert report["clusters_merged"] == 1
        assert report["memories_deprecated"] == 2
        store.store.assert_called_once()
        assert store.store.call_args[1]["source_pipeline"] == "dream_cycle"
        # 1 synthesized_from + 2 deprecated originals
        assert mock_update.call_count >= 3
        cursor = await db.execute(
            "SELECT status FROM deferred_work_queue WHERE work_type = ?",
            (WORKLIST_WORK_TYPE,),
        )
        rows = await cursor.fetchall()
        assert [r["status"] for r in rows] == ["completed"]


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

            await run(
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
    async def test_clustering_runs_in_thread_pool(self):
        """Clustering runs via asyncio.to_thread to avoid event loop starvation."""
        mock_qdrant = MagicMock()
        mock_db = AsyncMock()
        mock_router = AsyncMock()
        mock_store = AsyncMock()

        n_points = 10
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
             patch("genesis.memory.dream_cycle.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_scroll.return_value = (points, None)
            mock_vec.return_value = {
                f"p{i}": [0.1] * 768 for i in range(n_points)
            }
            # to_thread wraps both _scroll_and_group_sync and _cluster_bucket_sync
            # First call: _scroll_and_group → return buckets
            # Second call: _cluster_bucket → return empty clusters
            # Third call: entity resolution phase also calls _scroll_and_group
            mock_to_thread.side_effect = [
                {("memory", "store"): points},  # _scroll_and_group result (consolidation)
                [],  # _cluster_bucket result (no clusters)
                {("memory", "store"): points},  # _scroll_and_group result (entity resolution)
            ]

            await run(
                qdrant=mock_qdrant,
                db=mock_db,
                router=mock_router,
                store=mock_store,
                dry_run=True,
            )

        # scroll-and-group, cluster-bucket, and entity-resolution scroll should use to_thread
        assert mock_to_thread.call_count >= 2
        # First call should be _scroll_and_group_sync
        first_call_fn = mock_to_thread.call_args_list[0][0][0]
        assert first_call_fn.__name__ == "_scroll_and_group_sync"
