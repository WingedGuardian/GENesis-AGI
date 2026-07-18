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
    mock_crud.batch_created_at = AsyncMock(return_value={})
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
async def test_recall_threads_collection_episodic(mock_qdrant, mock_crud, mock_links, _):
    """A Qdrant hit from episodic_memory → result.collection == 'episodic_memory'
    (the authoritative first-party/external discriminator, audit D12)."""
    retriever, _, _, _ = _build_retriever()
    mock_qdrant.search.return_value = [_make_qdrant_hit("mem-1", 0.9)]
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    results = await retriever.recall(
        "test", source="episodic", limit=5, min_activation=0.0, rerank=False,
    )
    assert results
    assert results[0].collection == "episodic_memory"


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_threads_collection_knowledge(mock_qdrant, mock_crud, mock_links, _):
    """A Qdrant hit from knowledge_base → result.collection == 'knowledge_base'."""
    retriever, _, _, _ = _build_retriever()
    mock_qdrant.search.return_value = [_make_qdrant_hit("mem-1", 0.9)]
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    results = await retriever.recall(
        "test", source="knowledge", limit=5, min_activation=0.0, rerank=False,
    )
    assert results
    assert results[0].collection == "knowledge_base"


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_threads_collection_fts_only(mock_qdrant, mock_crud, mock_links, _):
    """An FTS5-only hit (no Qdrant vector) must still carry its collection from
    the FTS row — this is exactly the path where the old payload['scope'] signal
    was absent, so the collection discriminator must cover it (audit D12)."""
    retriever, _, _, _ = _build_retriever()
    mock_qdrant.search.return_value = []  # no vector hits → FTS-only
    mock_crud.search_ranked = AsyncMock(return_value=[
        {**_make_fts_row("mem-kb", -5.0), "collection": "knowledge_base"},
    ])
    mock_crud.batch_created_at = AsyncMock(return_value={})
    _setup_link_mocks(mock_links)

    results = await retriever.recall(
        "test", source="both", limit=5, min_activation=0.0, rerank=False,
    )
    assert results
    assert results[0].memory_id == "mem-kb"
    assert results[0].collection == "knowledge_base"


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
    mock_crud.batch_created_at = AsyncMock(return_value={})

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


# --- MEM-005: entrenchment signal (hand-rolled Spearman) ---


class TestSpearmanRankCorr:
    """_spearman_rank_corr powers the activation-entrenchment metric (MEM-005):
    does retrieval frequency predict final ranking? scipy is not a dependency,
    so it is hand-rolled."""

    def test_perfect_positive(self):
        from genesis.memory.retrieval import _spearman_rank_corr

        assert _spearman_rank_corr([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)

    def test_perfect_negative(self):
        from genesis.memory.retrieval import _spearman_rank_corr

        assert _spearman_rank_corr([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_none_when_too_short(self):
        from genesis.memory.retrieval import _spearman_rank_corr

        assert _spearman_rank_corr([1], [1]) is None
        assert _spearman_rank_corr([], []) is None

    def test_none_on_zero_variance(self):
        from genesis.memory.retrieval import _spearman_rank_corr

        # Constant xs → no ranking variance → undefined correlation.
        assert _spearman_rank_corr([5, 5, 5], [1, 2, 3]) is None

    def test_handles_ties_monotonic(self):
        from genesis.memory.retrieval import _spearman_rank_corr

        v = _spearman_rank_corr([1, 2, 2, 3], [10, 20, 20, 30])
        assert v == pytest.approx(1.0)

    def test_in_range(self):
        from genesis.memory.retrieval import _spearman_rank_corr

        v = _spearman_rank_corr([1, 5, 2, 8, 3], [2, 1, 4, 3, 9])
        assert -1.0 <= v <= 1.0


# --- Characterization tests: degradation paths + reranker contract ---
# Written BEFORE the recall() stage decomposition to pin current behavior.
# Each failure-path test asserts the failing dependency was actually called,
# so a refactor that silently skips the stage fails the test.


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test query")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_survives_event_calendar_failure(
    mock_qdrant, mock_crud, mock_links, _mock_expand,
):
    """A raising event-calendar range query degrades to a warning, not a crash."""
    retriever, _, _, _ = _build_retriever()
    mock_qdrant.search.return_value = [_make_qdrant_hit("mem-1", 0.95)]
    mock_crud.search_ranked = AsyncMock(return_value=[_make_fts_row("mem-1", -5.0)])
    _setup_link_mocks(mock_links)

    with (
        patch(
            "genesis.memory.temporal.parse_temporal_reference",
            return_value=("2026-07-01T00:00:00+00:00", "2026-07-02T00:00:00+00:00"),
        ),
        patch(
            "genesis.db.crud.memory_events.get_memory_ids_in_range",
            new_callable=AsyncMock,
            side_effect=RuntimeError("event calendar down"),
        ) as mock_events,
    ):
        results = await retriever.recall("when did that happen yesterday", limit=5)

    mock_events.assert_called_once()
    assert [r.memory_id for r in results] == ["mem-1"]


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock)
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_expansion_failure_uses_original_query(
    mock_qdrant, mock_crud, mock_links, mock_expand,
):
    """expand_query raising falls back to the ORIGINAL query, boolean=False."""
    retriever, _, _, _ = _build_retriever()
    mock_expand.side_effect = RuntimeError("tag index rebuild failed")
    mock_qdrant.search.return_value = [_make_qdrant_hit("mem-1", 0.95)]
    mock_crud.search_ranked = AsyncMock(return_value=[_make_fts_row("mem-1", -5.0)])
    _setup_link_mocks(mock_links)

    results = await retriever.recall("original query text", limit=5)

    mock_expand.assert_called_once()
    kwargs = mock_crud.search_ranked.call_args.kwargs
    assert kwargs["query"] == "original query text"
    assert kwargs["boolean"] is False
    assert len(results) == 1


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test query")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_expiry_filter_failure_returns_unfiltered(
    mock_qdrant, mock_crud, mock_links, _mock_expand,
):
    """A raising invalid_at lookup degrades to 'no expiry filter applied'."""
    retriever, _, _, _ = _build_retriever()
    mock_qdrant.search.return_value = [
        _make_qdrant_hit("mem-1", 0.95),
        _make_qdrant_hit("mem-2", 0.80),
    ]
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    with patch(
        "genesis.memory.retrieval._expired_candidate_ids",
        new_callable=AsyncMock,
        side_effect=RuntimeError("db locked"),
    ) as mock_expired:
        results = await retriever.recall("test query", limit=5)

    mock_expired.assert_called_once()
    assert {r.memory_id for r in results} == {"mem-1", "mem-2"}


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test query")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_update_payload_failure_still_returns_results(
    mock_qdrant, mock_crud, mock_links, _mock_expand,
):
    """A raising retrieved_count write-back never blocks returning results."""
    retriever, _, _, _ = _build_retriever()
    mock_qdrant.search.return_value = [_make_qdrant_hit("mem-1", 0.95)]
    mock_qdrant.update_payload.side_effect = RuntimeError("qdrant write failed")
    mock_crud.search_ranked = AsyncMock(return_value=[_make_fts_row("mem-1", -5.0)])
    _setup_link_mocks(mock_links)

    results = await retriever.recall("test query", limit=5)

    mock_qdrant.update_payload.assert_called_once()
    assert [r.memory_id for r in results] == ["mem-1"]


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test query")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_reranker_replaces_fused_with_positional_scores(
    mock_qdrant, mock_crud, mock_links, _mock_expand,
):
    """Reranker path: fused is REPLACED with 1/(1+rank) positional scores and
    candidates the reranker didn't score are dropped entirely."""
    embed_provider = MagicMock()
    embed_provider.embed = AsyncMock(return_value=[0.1] * 1024)
    reranker = MagicMock()
    reranker.enabled = True
    # Reranker inverts the RRF ordering and omits mem-3.
    reranker.rerank = AsyncMock(return_value=[{"id": "mem-2"}, {"id": "mem-1"}])
    retriever = HybridRetriever(
        embedding_provider=embed_provider,
        qdrant_client=MagicMock(),
        db=MagicMock(spec_set=["execute", "commit"]),
        reranker=reranker,
    )

    mock_qdrant.search.return_value = [
        _make_qdrant_hit("mem-1", 0.95),
        _make_qdrant_hit("mem-2", 0.80),
    ]
    mock_crud.search_ranked = AsyncMock(return_value=[_make_fts_row("mem-3", -5.0)])
    mock_crud.batch_created_at = AsyncMock(return_value={})
    _setup_link_mocks(mock_links)

    results = await retriever.recall("test query", limit=5, rerank=True)

    reranker.rerank.assert_called_once()
    call_args = reranker.rerank.call_args
    assert call_args.args[0] == "test query"
    assert {d["id"] for d in call_args.args[1]} == {"mem-1", "mem-2", "mem-3"}
    assert call_args.kwargs["top_k"] == 10  # limit * 2

    # Positional scores from the reranker's ordering; mem-3 dropped.
    assert [r.memory_id for r in results] == ["mem-2", "mem-1"]
    assert results[0].score == pytest.approx(1.0)   # 1/(1+0)
    assert results[1].score == pytest.approx(0.5)   # 1/(1+1)


# --- mem-007: diversity penalty must not contaminate J9-logged scores ---


def _make_echo_hit(mid: str, score: float, content: str) -> dict:
    hit = _make_qdrant_hit(mid, score)
    hit["payload"]["content"] = content
    return hit


@pytest.mark.asyncio
@patch("genesis.eval.j9_hooks.emit_recall_fired", new_callable=AsyncMock, return_value=None)
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_diversity_penalty_preserves_raw_score_for_j9(
    mock_qdrant, mock_crud, mock_links, _expand, mock_emit,
):
    """An echo-penalized result keeps its pre-penalty score in
    ``retrieval_score``, and the J-9 recall event logs THAT — not the
    halved dedup artifact. ``score`` (final ordering) stays penalized."""
    retriever, _, _, _ = _build_retriever()

    echo = "alpha beta gamma delta epsilon zeta identical content"
    mock_qdrant.search.return_value = [
        _make_echo_hit("echo-hi", 0.95, echo),
        _make_echo_hit("echo-lo", 0.90, echo),
    ]
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    results = await retriever.recall("test", limit=5)

    by_id = {r.memory_id: r for r in results}
    assert {"echo-hi", "echo-lo"} <= set(by_id), (
        "both echo-cluster members should survive (2 <= max_per_cluster)"
    )
    hi, lo = by_id["echo-hi"], by_id["echo-lo"]

    # The winner is unpenalized: final == raw
    assert hi.score == pytest.approx(hi.retrieval_score)
    # The echo is halved for ORDERING, but its raw score is preserved
    assert lo.score == pytest.approx(lo.retrieval_score * 0.5), (
        f"echo final score {lo.score} should be raw {lo.retrieval_score} * 0.5"
    )
    assert lo.retrieval_score > lo.score > 0.0

    # J-9 logging reads the RAW scores, not the penalized ones
    emit_kwargs = mock_emit.call_args.kwargs
    assert emit_kwargs["top_scores"] == pytest.approx(
        [r.retrieval_score for r in results[:5]]
    ), "J-9 top_scores must be pre-penalty retrieval scores"
    expected_mean = sum(r.retrieval_score for r in results) / len(results)
    assert emit_kwargs["mean_score"] == pytest.approx(expected_mean)


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_no_penalty_retrieval_score_equals_score(
    mock_qdrant, mock_crud, mock_links, _expand,
):
    """With no echo clusters, retrieval_score == score for every result."""
    retriever, _, _, _ = _build_retriever()

    mock_qdrant.search.return_value = [
        _make_echo_hit("m1", 0.95, "completely unique first content"),
        _make_echo_hit("m2", 0.90, "utterly different second thing"),
    ]
    mock_crud.search_ranked = AsyncMock(return_value=[])
    _setup_link_mocks(mock_links)

    results = await retriever.recall("test", limit=5)
    assert len(results) == 2
    for r in results:
        assert r.score == pytest.approx(r.retrieval_score)


# --- D4: FTS-only rows must not get phantom now_str freshness ---


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
async def test_compute_activations_fts_only_uses_metadata_created_at(
    mock_crud, mock_links,
):
    """D4: an FTS-only row (no Qdrant hit) takes its real created_at from
    memory_metadata via batch_created_at, not the now_str phantom that
    yields recency = exp(0) = 1.0 (and a phantom age of 0 in MEM-005)."""
    retriever, _, _, _ = _build_retriever()
    mock_links.batch_link_counts = AsyncMock(return_value={})
    old = "2020-01-01T00:00:00+00:00"
    mock_crud.batch_created_at = AsyncMock(return_value={"fts-1": old})

    now_str = datetime.now(UTC).isoformat()
    _act, _inb, _rc, created_at_by_id = await retriever._compute_activations(
        {"fts-1"}, {}, now_str,  # empty qdrant_by_id → fts-1 is FTS-only
    )
    assert created_at_by_id["fts-1"] == old  # real date, NOT now_str
    mock_crud.batch_created_at.assert_awaited_once_with(retriever._db, ["fts-1"])


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
async def test_compute_activations_fts_ghost_falls_back_to_now(
    mock_crud, mock_links,
):
    """A legacy FTS ghost with no metadata row keeps the now_str fallback."""
    retriever, _, _, _ = _build_retriever()
    mock_links.batch_link_counts = AsyncMock(return_value={})
    mock_crud.batch_created_at = AsyncMock(return_value={})  # no metadata row

    now_str = datetime.now(UTC).isoformat()
    _a, _i, _r, created_at_by_id = await retriever._compute_activations(
        {"ghost"}, {}, now_str,
    )
    assert created_at_by_id["ghost"] == now_str


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test query")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_invokes_entity_lane_shadow(mock_qdrant, mock_crud, mock_links, _mock_expand):
    """The entity-lane shadow probe is wired into recall() with the sets it
    needs (ranked_lists/all_ids/limit/embedding_available/recall_event_id)."""
    retriever, _, _, _ = _build_retriever()
    mock_qdrant.search.return_value = [_make_qdrant_hit("mem-1", 0.95)]
    mock_crud.search_ranked = AsyncMock(return_value=[_make_fts_row("mem-1", -5.0)])
    mock_crud.batch_created_at = AsyncMock(return_value={})
    _setup_link_mocks(mock_links, link_count=1)

    spy = AsyncMock(return_value=None)
    with patch("genesis.memory.entity_query.maybe_entity_lane_shadow", spy):
        results = await retriever.recall("test query", limit=10)

    assert len(results) > 0
    spy.assert_awaited_once()
    kw = spy.await_args.kwargs
    assert kw["query"] == "test query"
    assert isinstance(kw["ranked_lists"], list)
    assert isinstance(kw["all_ids"], set)
    assert kw["limit"] == 10
    assert isinstance(kw["embedding_available"], bool)
    assert "recall_event_id" in kw  # correlation id passed for joinable events


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test query")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_survives_entity_lane_shadow_failure(mock_qdrant, mock_crud, mock_links, _e):
    """A raising shadow helper must not change recall's output (the call site's
    own guard belts the helper's internal try/except)."""
    retriever, _, _, _ = _build_retriever()
    mock_qdrant.search.return_value = [_make_qdrant_hit("mem-1", 0.95), _make_qdrant_hit("mem-2", 0.8)]
    mock_crud.search_ranked = AsyncMock(return_value=[_make_fts_row("mem-1", -5.0)])
    mock_crud.batch_created_at = AsyncMock(return_value={})
    _setup_link_mocks(mock_links, link_count=1)

    # Baseline: real helper is a no-op (config ships mode: off).
    baseline = await retriever.recall("test query", limit=10)
    boom = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("genesis.memory.entity_query.maybe_entity_lane_shadow", boom):
        results = await retriever.recall("test query", limit=10)
    assert [r.memory_id for r in results] == [r.memory_id for r in baseline]


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test query")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_fires_entity_lane_shadow_on_zero_hits(mock_qdrant, mock_crud, mock_links, _e):
    """Even when vector/FTS/event find nothing (recall returns []), the entity
    lane shadow probe still fires with empty sets — its highest-value case
    (organic recall found nothing, the lane might still surface something)."""
    retriever, _, _, _ = _build_retriever()
    mock_qdrant.search.return_value = []  # no vector hits
    mock_crud.search_ranked = AsyncMock(return_value=[])  # no FTS hits
    mock_crud.batch_created_at = AsyncMock(return_value={})
    _setup_link_mocks(mock_links)

    spy = AsyncMock(return_value=None)
    with patch("genesis.memory.entity_query.maybe_entity_lane_shadow", spy):
        results = await retriever.recall("test query", limit=10)

    assert results == []  # zero organic candidates
    spy.assert_awaited_once()  # but the probe still ran
    kw = spy.await_args.kwargs
    assert kw["ranked_lists"] == []
    assert kw["all_ids"] == set()
