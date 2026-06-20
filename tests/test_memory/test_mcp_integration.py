"""Integration tests for memory_mcp tool implementations."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.mcp import memory_mcp
from genesis.mcp.memory_mcp import mcp as memory_mcp_server
from genesis.memory.types import RetrievalResult


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset module-level state before each test."""
    memory_mcp._store = None
    memory_mcp._retriever = None
    memory_mcp._user_model_evolver = None
    memory_mcp._db = None
    memory_mcp._qdrant = None
    yield
    memory_mcp._store = None
    memory_mcp._retriever = None
    memory_mcp._user_model_evolver = None
    memory_mcp._db = None
    memory_mcp._qdrant = None


@pytest.fixture
async def tools():
    """Get the MCP tool functions."""
    return await memory_mcp_server.get_tools()


@pytest.fixture
def mock_deps():
    """Return mocked db, qdrant, embedding_provider for init()."""
    db = MagicMock()
    qdrant = MagicMock()
    emb = MagicMock()
    return db, qdrant, emb


def _init_with_mocks(mock_deps):
    db, qdrant, emb = mock_deps
    memory_mcp.init(db=db, qdrant_client=qdrant, embedding_provider=emb)


# ─── Tests ────────────────────────────────────────────────────────────────────


def test_init_sets_state(mock_deps):
    _init_with_mocks(mock_deps)
    assert memory_mcp._store is not None
    assert memory_mcp._retriever is not None
    assert memory_mcp._db is not None
    assert memory_mcp._qdrant is not None
    assert memory_mcp._user_model_evolver is not None


@pytest.mark.asyncio
async def test_memory_store_delegates(mock_deps, tools):
    _init_with_mocks(mock_deps)
    memory_mcp._store = MagicMock()
    memory_mcp._store.store = AsyncMock(return_value="mem-123")

    result = await tools["memory_store"].fn(
        content="hello", source="test", memory_type="episodic"
    )
    assert result == "mem-123"
    memory_mcp._store.store.assert_awaited_once_with(
        "hello", "test", memory_type="episodic", tags=None, confidence=0.5,
        memory_class=None, source_pipeline="conversation",
        wing=None, room=None, collection=None, supersedes=None,
    )


@pytest.mark.asyncio
async def test_memory_recall_delegates(mock_deps, tools):
    _init_with_mocks(mock_deps)
    fake_result = RetrievalResult(
        memory_id="m1",
        content="test content",
        source="src",
        memory_type="episodic",
        score=0.9,
        vector_rank=1,
        fts_rank=2,
        activation_score=0.8,
        payload={},
    )
    memory_mcp._retriever = MagicMock()
    memory_mcp._retriever.recall = AsyncMock(return_value=[fake_result])

    results = await tools["memory_recall"].fn(query="query", source="both", limit=5)
    assert len(results) == 1
    assert results[0]["memory_id"] == "m1"
    memory_mcp._retriever.recall.assert_awaited_once_with(
        "query", source="both", limit=5, min_activation=0.0,
        wing=None, room=None, life_domain=None,
        expand_query_terms=True,
        include_subsystem=False, only_subsystem=None,
        rerank=True, include_deprecated=False,
    )


@pytest.mark.asyncio
async def test_memory_extract_multiple(mock_deps, tools):
    _init_with_mocks(mock_deps)
    memory_mcp._store = MagicMock()
    memory_mcp._store.store = AsyncMock(side_effect=["id-1", "id-2"])

    extractions = [
        {"content": "fact one", "source": "chat", "type": "fact"},
        {"content": "fact two", "confidence": 0.9},
    ]
    ids = await tools["memory_extract"].fn(extractions=extractions)
    assert ids == ["id-1", "id-2"]
    assert memory_mcp._store.store.await_count == 2


@pytest.mark.asyncio
async def test_memory_proactive_delegates(mock_deps, tools):
    _init_with_mocks(mock_deps)
    fake_result = RetrievalResult(
        memory_id="m2",
        content="proactive",
        source="src",
        memory_type="episodic",
        score=0.5,
        vector_rank=None,
        fts_rank=None,
        activation_score=0.3,
        payload={},
    )
    memory_mcp._retriever = MagicMock()
    memory_mcp._retriever.recall = AsyncMock(return_value=[fake_result])

    results = await tools["memory_proactive"].fn(current_message="hello world", limit=3)
    assert len(results) == 1
    memory_mcp._retriever.recall.assert_awaited_once_with(
        "hello world", limit=6, min_activation=0.0, rerank=False,
    )


@pytest.mark.asyncio
async def test_observation_write_creates(db, tools):
    """Use real db fixture to verify observation creation."""
    memory_mcp._db = db
    memory_mcp._store = MagicMock()
    memory_mcp._retriever = MagicMock()

    obs_id = await tools["observation_write"].fn(
        content="test observation",
        source="reflection",
        type="fact",
        priority="high",
    )
    assert isinstance(obs_id, str)

    from genesis.db.crud import observations

    row = await observations.get_by_id(db, obs_id)
    assert row is not None
    assert row["content"] == "test observation"
    assert row["priority"] == "high"


@pytest.mark.asyncio
async def test_observation_query_returns(db, tools):
    memory_mcp._db = db
    memory_mcp._store = MagicMock()
    memory_mcp._retriever = MagicMock()

    await tools["observation_write"].fn(
        content="obs1", source="test", type="fact", priority="low"
    )
    await tools["observation_write"].fn(
        content="obs2", source="test", type="decision", priority="high"
    )

    results = await tools["observation_query"].fn(type="fact")
    assert any(r["content"] == "obs1" for r in results)
    assert all(r["type"] == "fact" for r in results)


@pytest.mark.asyncio
async def test_observation_resolve_marks(db, tools):
    memory_mcp._db = db
    memory_mcp._store = MagicMock()
    memory_mcp._retriever = MagicMock()

    obs_id = await tools["observation_write"].fn(
        content="to resolve", source="test", type="fact"
    )
    ok = await tools["observation_resolve"].fn(
        observation_id=obs_id, resolution_notes="resolved it"
    )
    assert ok is True

    from genesis.db.crud import observations

    row = await observations.get_by_id(db, obs_id)
    assert row["resolved"] == 1
    assert row["resolution_notes"] == "resolved it"


@pytest.mark.asyncio
async def test_memory_stats_returns_dict(mock_deps, tools):
    _init_with_mocks(mock_deps)
    db_mock, qdrant_mock, _ = mock_deps

    with (
        patch(
            "genesis.mcp.memory_mcp.get_collection_info",
            side_effect=[
                {"points_count": 42, "status": "green"},
                {"points_count": 7, "status": "green"},
            ],
        ),
        patch(
            "genesis.mcp.memory_mcp.observations.query", new_callable=AsyncMock
        ) as mock_obs_q,
    ):
        mock_obs_q.return_value = []

        cursor_mock = AsyncMock()
        cursor_mock.fetchone = AsyncMock(return_value=(5,))
        db_mock.execute = AsyncMock(return_value=cursor_mock)
        db_mock.execute_fetchall = AsyncMock(return_value=[(5,)])

        result = await tools["memory_stats"].fn()

    assert result["episodic_count"] == 42
    assert result["knowledge_count"] == 7
    assert result["pending_deltas"] == 0
    assert result["total_links"] == 5


@pytest.mark.asyncio
async def test_uninitialized_raises(tools):
    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["memory_recall"].fn(query="test")

    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["memory_store"].fn(content="x", source="y")

    with pytest.raises(RuntimeError, match="not initialized"):
        await tools["observation_write"].fn(content="x", source="y", type="z")


# ─── WS-7 / D12: provenance surfacing ───────────────────────────────────────


def _rr(mid: str, *, collection: str, score: float = 0.9, pipeline: str | None = None):
    """Build a RetrievalResult with a given collection (provenance discriminator)."""
    return RetrievalResult(
        memory_id=mid, content=f"content {mid}", source="src",
        memory_type="knowledge" if collection == "knowledge_base" else "episodic",
        score=score, vector_rank=1, fts_rank=None, activation_score=0.5,
        payload={}, source_pipeline=pipeline, collection=collection,
    )


@pytest.mark.asyncio
async def test_memory_recall_labels_first_party_vs_external(mock_deps, tools):
    """memory_recall must tag each result's provenance so KB content reads as
    external-world and episodic reads as first-party (audit D12)."""
    _init_with_mocks(mock_deps)
    memory_mcp._retriever = MagicMock()
    memory_mcp._retriever.recall = AsyncMock(return_value=[
        _rr("ep1", collection="episodic_memory"),
        _rr("kb1", collection="knowledge_base", pipeline="curated"),
    ])

    results = await tools["memory_recall"].fn(
        query="q", source="both", limit=5, compact=True,
    )
    by_id = {r["memory_id"]: r for r in results}
    assert by_id["ep1"]["provenance"] == "first-party memory"
    assert by_id["kb1"]["provenance"].startswith("external-world knowledge")
    assert "user-curated" in by_id["kb1"]["provenance"]


@pytest.mark.asyncio
async def test_memory_recall_both_filter_drops_lowscore_kb_keeps_episodic(mock_deps, tools):
    """The source='both' KB score floor must key on the collection discriminator,
    not payload['scope'] — so a low-score KB hit (even FTS-only, no scope) is
    dropped while a low-score episodic hit is kept (audit D12 bug fix)."""
    _init_with_mocks(mock_deps)
    memory_mcp._retriever = MagicMock()
    memory_mcp._retriever.recall = AsyncMock(return_value=[
        _rr("kb_low", collection="knowledge_base", score=0.05),   # below 0.15 → drop
        _rr("kb_high", collection="knowledge_base", score=0.50),  # keep
        _rr("ep_low", collection="episodic_memory", score=0.05),  # keep (not external)
    ])

    results = await tools["memory_recall"].fn(
        query="q", source="both", limit=10, compact=True,
    )
    ids = {r["memory_id"] for r in results}
    assert "kb_low" not in ids
    assert "kb_high" in ids
    assert "ep_low" in ids


@pytest.mark.asyncio
async def test_memory_expand_labels_collection_and_provenance(mock_deps, tools):
    """memory_expand bypasses RetrievalResult — it must still tag each expanded
    item with its collection + provenance (audit D12)."""
    from types import SimpleNamespace

    _init_with_mocks(mock_deps)

    def _retrieve(collection_name, ids, with_payload):
        if collection_name == "knowledge_base":
            return [SimpleNamespace(
                id="kb1",
                payload={"content": "ext doc", "source": "api.pdf",
                         "source_pipeline": "curated", "memory_type": "knowledge"},
            )]
        return [SimpleNamespace(
            id="ep1",
            payload={"content": "my note", "source": "chat", "memory_type": "episodic"},
        )]

    memory_mcp._qdrant.retrieve = MagicMock(side_effect=_retrieve)

    results = await tools["memory_expand"].fn(memory_ids=["ep1", "kb1"])
    by_id = {r["memory_id"]: r for r in results if "memory_id" in r}
    assert by_id["ep1"]["collection"] == "episodic_memory"
    assert by_id["ep1"]["provenance"] == "first-party memory"
    assert by_id["kb1"]["collection"] == "knowledge_base"
    assert by_id["kb1"]["provenance"].startswith("external-world knowledge")


@pytest.mark.asyncio
async def test_knowledge_recall_labels_external(mock_deps, tools):
    """knowledge_recall is all-external by definition — every result must carry
    the external-world provenance label (audit D12)."""
    _init_with_mocks(mock_deps)
    memory_mcp._retriever = MagicMock()
    memory_mcp._retriever.recall = AsyncMock(return_value=[
        _rr("kb1", collection="knowledge_base", score=0.9, pipeline="recon"),
    ])

    results = await tools["knowledge_recall"].fn(query="q", limit=5, min_score=0.0)
    assert results
    assert results[0]["provenance"].startswith("external-world knowledge")
    assert "recon" in results[0]["provenance"]
