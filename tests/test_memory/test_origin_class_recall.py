"""WS-3 B4: stored ``origin_class`` is recovered by recall on BOTH pipelines.

The Qdrant path passes the payload value through; the FTS5-only path reads the
new ``memory_metadata.origin_class`` column carried on the ``search_ranked``
row dict (pre-B4 that whole provenance family was silently ``None`` on FTS-only
hits — the ``_p = {}`` gap). ``search_ranked`` itself is covered in
``tests/test_db/test_search_ranked_subsystem.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.retrieval import HybridRetriever


def _qdrant_hit(mid: str, score: float, *, origin_class: str | None) -> dict:
    payload = {
        "content": f"content for {mid}",
        "source": "test",
        "memory_type": "episodic",
        "tags": [],
        "confidence": 0.8,
        "created_at": datetime.now(UTC).isoformat(),
        "retrieved_count": 5,
        "source_type": "memory",
    }
    if origin_class is not None:
        payload["origin_class"] = origin_class
    return {"id": mid, "score": score, "payload": payload}


def _fts_row(mid: str, rank: float, *, origin_class: str | None) -> dict:
    return {
        "memory_id": mid,
        "content": f"fts content for {mid}",
        "source_type": "memory",
        "collection": "episodic_memory",
        "rank": rank,
        "origin_class": origin_class,
    }


def _build_retriever() -> HybridRetriever:
    embed_provider = MagicMock()
    embed_provider.embed = AsyncMock(return_value=[0.1] * 1024)
    return HybridRetriever(
        embedding_provider=embed_provider,
        qdrant_client=MagicMock(),
        db=MagicMock(spec_set=["execute", "commit"]),
    )


def _link_mocks(mock_links) -> None:
    mock_links.count_links = AsyncMock(return_value=0)
    mock_links.batch_link_counts = AsyncMock(return_value={})
    mock_links.inter_candidate_links = AsyncMock(return_value=[])


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_qdrant_hit_carries_stored_origin(mock_qdrant, mock_crud, mock_links, _):
    retriever = _build_retriever()
    mock_qdrant.search.return_value = [
        _qdrant_hit("ext-1", 0.9, origin_class="external_untrusted"),
        _qdrant_hit("fp-1", 0.8, origin_class="first_party"),
    ]
    mock_crud.search_ranked = AsyncMock(return_value=[])
    mock_crud.batch_created_at = AsyncMock(return_value={})
    _link_mocks(mock_links)

    results = await retriever.recall(
        "test",
        source="episodic",
        limit=5,
        min_activation=0.0,
        rerank=False,
    )
    by_id = {r.memory_id: r for r in results}
    assert by_id["ext-1"].origin_class == "external_untrusted"
    assert by_id["fp-1"].origin_class == "first_party"


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_fts_only_hit_carries_stored_origin(mock_qdrant, mock_crud, mock_links, _):
    """The pre-B4 gap: an FTS5-only hit lost every payload-derived field."""
    retriever = _build_retriever()
    mock_qdrant.search.return_value = []
    mock_crud.search_ranked = AsyncMock(
        return_value=[
            _fts_row("fts-ext", -5.0, origin_class="external_untrusted"),
        ]
    )
    mock_crud.batch_created_at = AsyncMock(return_value={})
    _link_mocks(mock_links)

    results = await retriever.recall(
        "test",
        source="episodic",
        limit=5,
        min_activation=0.0,
        rerank=False,
    )
    assert results
    assert results[0].origin_class == "external_untrusted"
    # source_pipeline stays honestly unrecoverable on the FTS path.
    assert results[0].source_pipeline is None


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_missing_stored_origin_is_none(mock_qdrant, mock_crud, mock_links, _):
    """Pre-0054 rows (no stamped value) surface as None — the consumer
    falls back to (collection, source_pipeline) re-derivation."""
    retriever = _build_retriever()
    mock_qdrant.search.return_value = [_qdrant_hit("old-1", 0.9, origin_class=None)]
    mock_crud.search_ranked = AsyncMock(
        return_value=[
            _fts_row("old-2", -5.0, origin_class=None),
        ]
    )
    mock_crud.batch_created_at = AsyncMock(return_value={})
    # The 12b backfill queries metadata for the None rows and finds nothing
    # (genuinely unstamped pre-0054 rows stay None).
    mock_crud.origin_class_by_ids = AsyncMock(return_value={})
    _link_mocks(mock_links)

    results = await retriever.recall(
        "test",
        source="episodic",
        limit=5,
        min_activation=0.0,
        rerank=False,
    )
    assert results
    assert all(r.origin_class is None for r in results)
    mock_crud.origin_class_by_ids.assert_awaited()


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_vector_only_stale_payload_backfills_from_metadata(
    mock_qdrant, mock_crud, mock_links, _
):
    """Codex on #1048 (round 5): a VECTOR-ONLY hit whose Qdrant payload
    predates the origin backfill has no FTS row to coalesce from — recall
    must batch-recover the stored value from memory_metadata so the
    injection gate can't be bypassed by a stale payload."""
    retriever = _build_retriever()
    mock_qdrant.search.return_value = [_qdrant_hit("stale-1", 0.9, origin_class=None)]
    mock_crud.search_ranked = AsyncMock(return_value=[])
    mock_crud.batch_created_at = AsyncMock(return_value={})
    mock_crud.origin_class_by_ids = AsyncMock(return_value={"stale-1": "external_untrusted"})
    _link_mocks(mock_links)

    results = await retriever.recall(
        "test",
        source="episodic",
        limit=5,
        min_activation=0.0,
        rerank=False,
    )
    assert results
    assert results[0].origin_class == "external_untrusted"
    mock_crud.origin_class_by_ids.assert_awaited_once()


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_qdrant_payload_missing_coalesces_to_fts_row(mock_qdrant, mock_crud, mock_links, _):
    """A Qdrant hit whose payload predates the 0054 backfill coalesces the
    stored class from the joined FTS/SQLite row (Codex P2 on #1042)."""
    retriever = _build_retriever()
    mock_qdrant.search.return_value = [_qdrant_hit("dual-1", 0.9, origin_class=None)]
    mock_crud.search_ranked = AsyncMock(
        return_value=[_fts_row("dual-1", -5.0, origin_class="first_party")]
    )
    mock_crud.batch_created_at = AsyncMock(return_value={})
    _link_mocks(mock_links)

    results = await retriever.recall(
        "test",
        source="episodic",
        limit=5,
        min_activation=0.0,
        rerank=False,
    )
    assert results
    assert results[0].origin_class == "first_party"
