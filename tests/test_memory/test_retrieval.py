from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.retrieval import HybridRetriever, _rrf_fuse

# --- RRF unit tests ---


def test_rrf_fuse_basic():
    """Two lists with overlapping items produce correct fused scores."""
    list_a = ["a", "b", "c"]
    list_b = ["b", "c", "d"]
    scores = _rrf_fuse([list_a, list_b], k=60)

    # "b" appears at rank 2 in list_a, rank 1 in list_b
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    # "a" only in list_a at rank 1
    assert scores["a"] == pytest.approx(1 / 61)
    # "d" only in list_b at rank 3
    assert scores["d"] == pytest.approx(1 / 63)
    # "b" should have the highest score
    assert scores["b"] > scores["a"]
    assert scores["b"] > scores["c"]


def test_rrf_fuse_single_list():
    """Single list produces simple reciprocal rank scores."""
    scores = _rrf_fuse([["x", "y", "z"]], k=60)
    assert scores["x"] == pytest.approx(1 / 61)
    assert scores["y"] == pytest.approx(1 / 62)
    assert scores["z"] == pytest.approx(1 / 63)


def test_rrf_fuse_no_overlap():
    """Disjoint lists produce scores for all items."""
    scores = _rrf_fuse([["a", "b"], ["c", "d"]], k=60)
    assert len(scores) == 4
    for mid in ("a", "b", "c", "d"):
        assert mid in scores


# --- Recall integration tests (all deps mocked) ---


def _make_qdrant_hit(mid: str, score: float, *, confidence: float = 0.8) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "id": mid,
        "score": score,
        "payload": {
            "content": f"content for {mid}",
            "source": "test",
            "memory_type": "episodic",
            "tags": [],
            "confidence": confidence,
            "created_at": now,
            "retrieved_count": 5,
            "source_type": "memory",
        },
    }


def _make_fts_row(mid: str, rank: float) -> dict:
    return {
        "memory_id": mid,
        "content": f"fts content for {mid}",
        "source_type": "memory",
        "collection": "episodic_memory",
        "rank": rank,
    }


def _setup_link_mocks(mock_links, *, link_count: int = 0, batch: dict | None = None):
    """Configure memory_links mocks for both old count_links and new batch APIs."""
    mock_links.count_links = AsyncMock(return_value=link_count)
    mock_links.batch_link_counts = AsyncMock(return_value=batch or {})
    mock_links.inter_candidate_links = AsyncMock(return_value=[])


def _build_retriever():
    embed_provider = MagicMock()
    embed_provider.embed = AsyncMock(return_value=[0.1] * 1024)
    qdrant_client = MagicMock()
    db = MagicMock(spec_set=["execute", "commit"])
    return HybridRetriever(
        embedding_provider=embed_provider,
        qdrant_client=qdrant_client,
        db=db,
    ), embed_provider, qdrant_client, db


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test query")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_returns_results(mock_qdrant, mock_crud, mock_links, _mock_expand):
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = [
        _make_qdrant_hit("mem-1", 0.95),
        _make_qdrant_hit("mem-2", 0.80),
    ]
    mock_crud.search_ranked = AsyncMock(return_value=[
        _make_fts_row("mem-1", -5.0),
        _make_fts_row("mem-3", -3.0),
    ])
    _setup_link_mocks(mock_links, link_count=2)

    results = await retriever.recall("test query", limit=10)
    assert len(results) > 0
    assert all(hasattr(r, "memory_id") for r in results)
    # V4 groundwork: intent fields should be populated
    assert all(r.query_intent is not None for r in results)


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_episodic_only(mock_qdrant, mock_crud, mock_links, _):
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = [_make_qdrant_hit("mem-1", 0.9)]
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    await retriever.recall("test", source="episodic", limit=5)

    # Should only search episodic_memory collection
    assert mock_qdrant.search.call_count == 1
    call_kwargs = mock_qdrant.search.call_args
    assert call_kwargs.kwargs["collection"] == "episodic_memory"


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_knowledge_only(mock_qdrant, mock_crud, mock_links, _):
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = [_make_qdrant_hit("mem-1", 0.9)]
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    await retriever.recall("test", source="knowledge", limit=5)

    assert mock_qdrant.search.call_count == 1
    assert mock_qdrant.search.call_args.kwargs["collection"] == "knowledge_base"


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_both_sources(mock_qdrant, mock_crud, mock_links, _):
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = [_make_qdrant_hit("mem-1", 0.9)]
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    await retriever.recall("test", source="both", limit=5)

    # Should search both collections
    assert mock_qdrant.search.call_count == 2
    collections_searched = [
        c.kwargs["collection"] for c in mock_qdrant.search.call_args_list
    ]
    assert "episodic_memory" in collections_searched
    assert "knowledge_base" in collections_searched


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_fts5_collection_filter_matches_source_episodic(
    mock_qdrant, mock_crud, mock_links, _,
):
    """FTS5 must filter to the source collection when source is single
    — regression guard for the bug where collection=None was hardcoded
    and knowledge_base entries leaked into episodic recall."""
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = []
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    await retriever.recall("test", source="episodic", limit=5)

    mock_crud.search_ranked.assert_called_once()
    assert mock_crud.search_ranked.call_args.kwargs["collection"] == "episodic_memory"


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_fts5_collection_filter_matches_source_knowledge(
    mock_qdrant, mock_crud, mock_links, _,
):
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = []
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    await retriever.recall("test", source="knowledge", limit=5)

    mock_crud.search_ranked.assert_called_once()
    assert mock_crud.search_ranked.call_args.kwargs["collection"] == "knowledge_base"


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_fts5_collection_filter_both_searches_all(
    mock_qdrant, mock_crud, mock_links, _,
):
    """When source='both', FTS5 still searches everything (None) — RRF
    fuses the union with Qdrant's two-collection result."""
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = []
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    await retriever.recall("test", source="both", limit=5)

    mock_crud.search_ranked.assert_called_once()
    assert mock_crud.search_ranked.call_args.kwargs["collection"] is None


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="why decided x")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_source_none_routes_by_intent_episodic(
    mock_qdrant, mock_crud, mock_links, _,
):
    """When source=None (default), WHY queries route to episodic only."""
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = []
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    # WHY intent → recommended_source = 'episodic'
    await retriever.recall("why did we decide x?", limit=5)

    assert mock_qdrant.search.call_count == 1
    assert mock_qdrant.search.call_args.kwargs["collection"] == "episodic_memory"
    assert mock_crud.search_ranked.call_args.kwargs["collection"] == "episodic_memory"


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="what is x")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_source_none_routes_by_intent_both(
    mock_qdrant, mock_crud, mock_links, _,
):
    """WHAT queries route to both via intent default."""
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = []
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    await retriever.recall("what is the cc_relay?", limit=5)

    # Both collections searched in Qdrant; FTS5 collection=None
    assert mock_qdrant.search.call_count == 2
    assert mock_crud.search_ranked.call_args.kwargs["collection"] is None


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="general query")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_explicit_source_overrides_intent(
    mock_qdrant, mock_crud, mock_links, _,
):
    """Caller passing source='knowledge' on a WHY query gets knowledge,
    not the WHY-recommended episodic. Explicit beats inferred."""
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = []
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    # WHY intent would route episodic, but caller forces knowledge
    await retriever.recall("why did we decide x?", source="knowledge", limit=5)

    assert mock_qdrant.search.call_count == 1
    assert mock_qdrant.search.call_args.kwargs["collection"] == "knowledge_base"
    assert mock_crud.search_ranked.call_args.kwargs["collection"] == "knowledge_base"


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_unknown_source_raises(mock_qdrant, mock_crud, mock_links, _):
    """Unknown source string still rejected after the source=None handling."""
    retriever, _, _, _ = _build_retriever()
    with pytest.raises(ValueError, match="source must be one of"):
        await retriever.recall("anything", source="bogus", limit=5)


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_min_activation_filters(mock_qdrant, mock_crud, mock_links, _):
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = [
        _make_qdrant_hit("mem-1", 0.95, confidence=0.01),
        _make_qdrant_hit("mem-2", 0.80, confidence=0.01),
    ]
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    # Very high min_activation should filter out low-confidence results
    results = await retriever.recall("test", min_activation=0.99, limit=10)
    assert len(results) == 0


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_increments_retrieved_count(mock_qdrant, mock_crud, mock_links, _):
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = [_make_qdrant_hit("mem-1", 0.95)]
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    await retriever.recall("test", limit=5)

    # update_payload should be called for returned results
    mock_qdrant.update_payload.assert_called()
    call_kwargs = mock_qdrant.update_payload.call_args.kwargs
    assert call_kwargs["point_id"] == "mem-1"
    assert call_kwargs["payload"]["retrieved_count"] == 6  # was 5, now 6


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_empty_results(mock_qdrant, mock_crud, mock_links, _):
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = []
    mock_crud.search_ranked = AsyncMock(return_value=[])

    results = await retriever.recall("test", limit=10)
    assert results == []


# --- Intent routing integration tests ---


def _make_qdrant_hit_with_meta(
    mid: str, score: float, *, source: str = "test", tags: list[str] | None = None,
    content: str = "", confidence: float = 0.8,
) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "id": mid,
        "score": score,
        "payload": {
            "content": content or f"content for {mid}",
            "source": source,
            "memory_type": "episodic",
            "tags": tags or [],
            "confidence": confidence,
            "created_at": now,
            "retrieved_count": 1,
            "source_type": "memory",
        },
    }


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="why did we choose subprocess")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_why_intent_boosts_decision_memories(
    mock_qdrant, mock_crud, mock_links, _mock_expand,
):
    """WHY query should boost memories with decision tags and reflection sources."""
    retriever, _, _, _ = _build_retriever()

    # mem-decision: has WHY-relevant metadata (deep_reflection + decision tag)
    # mem-generic: generic memory with same vector score
    mock_qdrant.search.return_value = [
        _make_qdrant_hit_with_meta(
            "mem-decision", 0.85,
            source="deep_reflection", tags=["decision"],
            content="we decided to use subprocess because of reliability",
        ),
        _make_qdrant_hit_with_meta(
            "mem-generic", 0.90,  # Higher vector score
            source="session_extraction", tags=["entity"],
            content="subprocess is a Python module",
        ),
    ]
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    results = await retriever.recall("why did we choose subprocess?", limit=2)
    assert len(results) == 2
    # Intent routing should boost mem-decision despite lower vector score
    assert results[0].query_intent == "WHY"
    assert results[0].intent_confidence > 0


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="subprocess popen error")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_general_intent_no_bias(mock_qdrant, mock_crud, mock_links, _mock_expand):
    """GENERAL query (no intent prefix) should produce no intent bias."""
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = [
        _make_qdrant_hit("mem-1", 0.95),
        _make_qdrant_hit("mem-2", 0.80),
    ]
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    results = await retriever.recall("subprocess popen error", limit=2)
    assert len(results) > 0
    assert results[0].query_intent == "GENERAL"
    assert results[0].intent_confidence == 0.0


# --- Subsystem filter threading (Phase 1.5b) ---


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_default_excludes_subsystems(
    mock_qdrant, mock_crud, mock_links, _,
):
    """Default recall must pass exclude=[ego,triage,reflection] to both stores."""
    retriever, _, _, _ = _build_retriever()
    mock_qdrant.search.return_value = []
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    await retriever.recall("what is x", limit=5)

    q_kwargs = mock_qdrant.search.call_args.kwargs
    assert q_kwargs["exclude_subsystems"] == ["ego", "triage", "reflection", "autonomy"]
    assert q_kwargs["include_only_subsystems"] is None
    f_kwargs = mock_crud.search_ranked.call_args.kwargs
    assert f_kwargs["exclude_subsystems"] == ["ego", "triage", "reflection", "autonomy"]
    assert f_kwargs["include_only_subsystems"] is None


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_include_subsystem_true_no_filter(
    mock_qdrant, mock_crud, mock_links, _,
):
    retriever, _, _, _ = _build_retriever()
    mock_qdrant.search.return_value = []
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    await retriever.recall("what is x", limit=5, include_subsystem=True)

    q_kwargs = mock_qdrant.search.call_args.kwargs
    assert q_kwargs["exclude_subsystems"] is None
    assert q_kwargs["include_only_subsystems"] is None


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_include_subsystem_list_keeps_ego(
    mock_qdrant, mock_crud, mock_links, _,
):
    """include_subsystem=['ego'] should still exclude triage+reflection."""
    retriever, _, _, _ = _build_retriever()
    mock_qdrant.search.return_value = []
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    await retriever.recall("x", limit=5, include_subsystem=["ego"])

    q_kwargs = mock_qdrant.search.call_args.kwargs
    assert q_kwargs["exclude_subsystems"] == ["triage", "reflection", "autonomy"]
    assert q_kwargs["include_only_subsystems"] is None


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_only_subsystem_inverts_filter(
    mock_qdrant, mock_crud, mock_links, _,
):
    """only_subsystem='ego' should produce include_only=['ego']."""
    retriever, _, _, _ = _build_retriever()
    mock_qdrant.search.return_value = []
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    await retriever.recall("x", limit=5, only_subsystem="ego")

    q_kwargs = mock_qdrant.search.call_args.kwargs
    assert q_kwargs["exclude_subsystems"] is None
    assert q_kwargs["include_only_subsystems"] == ["ego"]


@pytest.mark.asyncio
async def test_recall_mutually_exclusive_params() -> None:
    """Passing both include_subsystem and only_subsystem must raise."""
    retriever, _, _, _ = _build_retriever()
    with pytest.raises(ValueError, match="mutually exclusive"):
        await retriever.recall(
            "x", limit=5,
            include_subsystem=["ego"], only_subsystem="triage",
        )


# --- Graph-boosted retrieval tests ---


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_graph_boost_backlink(mock_qdrant, mock_crud, mock_links, _):
    """Memories with more inbound links get higher fused scores via backlink boost."""
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = [
        _make_qdrant_hit("mem-low", 0.90),
        _make_qdrant_hit("mem-high", 0.89),  # slightly lower vector score
    ]
    mock_crud.search_ranked = AsyncMock(return_value=[])

    # mem-high has 20 inbound links; mem-low has 0
    mock_links.batch_link_counts = AsyncMock(return_value={
        "mem-low": (0, 0),
        "mem-high": (20, 20),
    })
    mock_links.inter_candidate_links = AsyncMock(return_value=[])
    mock_links.count_links = AsyncMock(return_value=0)

    results = await retriever.recall("test", limit=2)
    assert len(results) == 2
    # mem-high should be boosted above mem-low despite lower vector score
    assert results[0].memory_id == "mem-high"


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_graph_boost_floor_gating(mock_qdrant, mock_crud, mock_links, _):
    """Low-scoring results should NOT receive graph boosts (floor gate).

    To create a genuine fused-score gap: s1-s3 appear in Qdrant AND FTS
    (contributing to 3 ranked lists), while weak appears ONLY in FTS
    (contributing to 2 lists: fts + activation). This gives weak a
    meaningfully lower fused score that falls below the 85% floor.
    """
    retriever, _, _, _ = _build_retriever()

    # weak NOT in Qdrant results — only s1-s3
    mock_qdrant.search.return_value = [
        _make_qdrant_hit("s1", 0.95),
        _make_qdrant_hit("s2", 0.90),
        _make_qdrant_hit("s3", 0.85),
    ]
    # weak appears only in FTS with the worst rank
    mock_crud.search_ranked = AsyncMock(return_value=[
        _make_fts_row("s1", -10.0),
        _make_fts_row("s2", -8.0),
        _make_fts_row("s3", -6.0),
        _make_fts_row("weak", -1.0),
    ])

    # weak has massive inbound links — but should be floor-gated
    mock_links.batch_link_counts = AsyncMock(return_value={
        "s1": (0, 0),
        "s2": (0, 0),
        "s3": (0, 0),
        "weak": (0, 200),
    })
    mock_links.inter_candidate_links = AsyncMock(return_value=[])
    mock_links.count_links = AsyncMock(return_value=0)

    results = await retriever.recall("test", limit=4)
    assert len(results) == 4
    # s1 must still rank first — floor gating prevents weak's boost
    assert results[0].memory_id == "s1"
    # weak should remain last despite 200 inbound links
    assert results[-1].memory_id == "weak"


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_graph_boost_adjacency(mock_qdrant, mock_crud, mock_links, _):
    """Adjacency boost rewards memories that link to each other in top-K."""
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = [
        _make_qdrant_hit("a", 0.90),
        _make_qdrant_hit("b", 0.89),
        _make_qdrant_hit("c", 0.88),
    ]
    mock_crud.search_ranked = AsyncMock(return_value=[])

    mock_links.batch_link_counts = AsyncMock(return_value={
        "a": (0, 0),
        "b": (0, 0),
        "c": (2, 2),  # c has 2 inbound overall
    })
    # Both a and b link to c within the top-K set
    mock_links.inter_candidate_links = AsyncMock(return_value=[
        ("a", "c"),
        ("b", "c"),
    ])
    mock_links.count_links = AsyncMock(return_value=0)

    results = await retriever.recall("test", limit=3)
    assert len(results) == 3
    # c should get both backlink boost and adjacency boost
    c_result = next(r for r in results if r.memory_id == "c")
    a_result = next(r for r in results if r.memory_id == "a")
    # c's boosted score should exceed a's (despite lower vector score)
    assert c_result.score > a_result.score
