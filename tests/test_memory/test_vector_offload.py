"""Qdrant vector search runs off the event loop (follow-up ac27b693, PR-2).

Qdrant's client is synchronous; `_gather_vector_candidates` now runs the whole
per-collection search loop in a worker thread via `asyncio.to_thread` so it never
blocks the shared event loop under concurrent recalls. These tests pin (a) that
the blocking search actually executes off the main thread and (b) that the
score-sort / `_collection`-tag / first-hit-wins dedup behavior is unchanged.
"""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.retrieval import HybridRetriever


def _retriever() -> HybridRetriever:
    embed = MagicMock()
    embed.embed = AsyncMock(return_value=[0.1] * 1024)
    return HybridRetriever(
        embedding_provider=embed,
        qdrant_client=MagicMock(),
        db=MagicMock(),
    )


def _hit(mid: str, score: float) -> dict:
    return {"id": mid, "score": score, "payload": {"content": mid}}


async def _gather(retriever: HybridRetriever, collections: list[str]):
    return await retriever._gather_vector_candidates(
        vector=[0.1] * 1024,
        collections=collections,
        candidate_limit=10,
        wing=None,
        room=None,
        life_domain=None,
        project_type=None,
        exclude_subsystems=None,
        include_only_subsystems=None,
        include_deprecated=False,
    )


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_search_runs_off_event_loop(mock_qdrant):
    """The synchronous qdrant_ops.search executes in a worker thread, not on the
    event-loop thread (that is the whole point of the to_thread offload)."""
    main_ident = threading.get_ident()
    seen: dict = {}

    def _search(_client, *, collection, **_kw):
        seen["ident"] = threading.get_ident()
        return [_hit("m1", 0.9)]

    mock_qdrant.search.side_effect = _search

    results, by_id = await _gather(_retriever(), ["c1"])

    assert "ident" in seen, "search was never called"
    assert seen["ident"] != main_ident, "search ran on the event-loop thread (not offloaded)"
    assert [h["id"] for h in results] == ["m1"]
    assert by_id["m1"]["_collection"] == "c1"


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_sort_tag_dedup_parity(mock_qdrant):
    """Across collections: results stay score-sorted desc, every hit is
    `_collection`-tagged, and the dedup map keeps the first hit in score order
    (so the higher-scoring duplicate wins) — identical to the pre-offload loop."""

    def _search(_client, *, collection, **_kw):
        if collection == "c1":
            return [_hit("dup", 0.5), _hit("m1", 0.9)]
        return [_hit("dup", 0.8), _hit("m2", 0.7)]

    mock_qdrant.search.side_effect = _search

    results, by_id = await _gather(_retriever(), ["c1", "c2"])

    scores = [h["score"] for h in results]
    assert scores == sorted(scores, reverse=True)
    assert all("_collection" in h for h in results)
    # first-hit-wins after the score sort → dup's 0.8 (from c2) beats its 0.5
    assert by_id["dup"]["score"] == 0.8
    assert by_id["dup"]["_collection"] == "c2"
    assert set(by_id) == {"dup", "m1", "m2"}
