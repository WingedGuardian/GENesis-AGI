"""Recall side-effect deferral + sub-stage telemetry (follow-up ac27b693).

The proactive per-prompt path passes ``defer_side_effects=True`` so recall's
write-backs and eval emits run on a background task AFTER results return — off
the 4.5s route budget. Deep-search callers (default False) keep them inline.
These tests pin both halves of that contract plus the new per-stage timings.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.retrieval import HybridRetriever


def _qdrant_hit(mid: str, score: float) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "id": mid,
        "score": score,
        "payload": {
            "content": f"content for {mid}",
            "source": "test",
            "memory_type": "episodic",
            "tags": [],
            "confidence": 0.8,
            "created_at": now,
            "retrieved_count": 5,
            "source_type": "memory",
        },
    }


def _build_retriever():
    embed = MagicMock()
    embed.embed = AsyncMock(return_value=[0.1] * 1024)
    db = MagicMock(spec_set=["execute", "commit"])
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    return HybridRetriever(
        embedding_provider=embed,
        qdrant_client=MagicMock(),
        db=db,
    )


def _wire(mock_qdrant, mock_crud, mock_links):
    mock_qdrant.search.return_value = [_qdrant_hit("mem-1", 0.95)]
    mock_qdrant.update_payload = MagicMock()
    mock_crud.search_ranked = AsyncMock(return_value=[])
    mock_crud.batch_created_at = AsyncMock(return_value={})
    mock_links.batch_link_counts = AsyncMock(return_value={})
    mock_links.inter_candidate_links = AsyncMock(return_value=[])


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_deferred_writeback_runs_after_return(mock_qdrant, mock_crud, mock_links, _exp):
    """defer_side_effects=True: results return BEFORE the Qdrant write-back fires;
    the write-back then runs on the background task."""
    retriever = _build_retriever()
    _wire(mock_qdrant, mock_crud, mock_links)

    results = await retriever.recall("test", limit=5, defer_side_effects=True)
    assert results  # got results synchronously
    # The retrieved_count write-back has NOT fired yet — it's deferred.
    assert mock_qdrant.update_payload.call_count == 0

    # Let the background task run.
    for _ in range(20):
        if mock_qdrant.update_payload.call_count > 0:
            break
        await asyncio.sleep(0.02)
    assert mock_qdrant.update_payload.call_count >= 1


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_inline_writeback_is_synchronous(mock_qdrant, mock_crud, mock_links, _exp):
    """Default path (deep search): the write-back fires INLINE before recall
    returns — byte-for-byte the pre-change behavior."""
    retriever = _build_retriever()
    _wire(mock_qdrant, mock_crud, mock_links)

    await retriever.recall("test", limit=5)  # defer_side_effects defaults False
    # Already fired by the time recall returned — no background wait needed.
    assert mock_qdrant.update_payload.call_count >= 1


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_backstop_falls_back_to_inline_at_cap(mock_qdrant, mock_crud, mock_links, _exp):
    """When the in-flight backstop is saturated, defer_side_effects=True runs the
    write-back INLINE (synchronously) rather than piling up another task — a
    write is never dropped, memory can't grow unbounded."""
    from genesis.memory import retrieval

    retriever = _build_retriever()
    _wire(mock_qdrant, mock_crud, mock_links)

    saved = retrieval._deferred_side_effects_count
    retrieval._deferred_side_effects_count = retrieval._DEFERRED_SIDE_EFFECT_CAP
    try:
        await retriever.recall("test", limit=5, defer_side_effects=True)
        # At cap → inline: the write-back already fired, no background wait.
        assert mock_qdrant.update_payload.call_count >= 1
    finally:
        retrieval._deferred_side_effects_count = saved


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_stats_carries_substage_timings(mock_qdrant, mock_crud, mock_links, _exp):
    """A caller-owned stats dict is populated with per-stage read timings."""
    retriever = _build_retriever()
    _wire(mock_qdrant, mock_crud, mock_links)

    stats: dict = {}
    await retriever.recall("test", limit=5, stats=stats, defer_side_effects=True)
    for key in ("vector_ms", "expand_ms", "fts_ms", "activation_ms"):
        assert key in stats, f"missing {key} in {stats}"
        assert isinstance(stats[key], float)

    # Drain the deferred task so it doesn't outlive the test.
    for _ in range(20):
        if mock_qdrant.update_payload.call_count > 0:
            break
        await asyncio.sleep(0.02)
