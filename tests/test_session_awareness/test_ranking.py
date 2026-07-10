"""Ranking unit tests — mocked lanes, real formula, zero-write proof."""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.session_awareness.ranking import (
    BACKLINK_COEF,
    DEFAULT_CONFIDENCE,
    TOP_N,
    rank_candidates,
)

DIM = 8
EMA = [1.0] + [0.0] * (DIM - 1)


def _vec(cos_target: float) -> list[float]:
    """A unit vector at the given cosine to EMA (axis 0)."""
    return [cos_target, math.sqrt(max(0.0, 1 - cos_target**2))] + [0.0] * (DIM - 2)


def _hit(mid: str, score: float, *, confidence=0.8, mclass="fact", content="c"):
    return {
        "id": mid,
        "score": score,
        "payload": {
            "confidence": confidence,
            "memory_class": mclass,
            "content": content,
        },
    }


def _rr(mid: str, *, confidence=0.6, mclass="rule"):
    r = MagicMock()
    r.memory_id = mid
    r.content = f"drift {mid}"
    r.payload = {"confidence": confidence, "memory_class": mclass}
    return r


def _db():
    return MagicMock()  # only passed through to patched helpers


@pytest.mark.asyncio
async def test_formula_and_ordering():
    qdrant = MagicMock()
    qdrant.retrieve = MagicMock(return_value=[])
    with (
        patch("genesis.qdrant.collections.search", side_effect=[[
            _hit("high", 0.9, confidence=1.0, mclass="rule"),
            _hit("mid", 0.9, confidence=0.5, mclass="fact"),
            _hit("low", 0.2, confidence=1.0, mclass="reference"),
        ], []]),
        patch("genesis.memory.drift.drift_recall", new=AsyncMock(return_value=[])),
        patch(
            "genesis.memory.retrieval._expired_candidate_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "genesis.db.crud.memory_links.batch_link_counts",
            new=AsyncMock(return_value={"high": (5, 3), "mid": (0, 0)}),
        ),
    ):
        ranked = await rank_candidates(
            ema=EMA, entity_query="q", db=_db(),
            qdrant_client=qdrant, embedding_provider=MagicMock(),
        )
    by_id = {c["memory_id"]: c for c in ranked}
    # high: (1 + 0.05*log1p(3)) * 1.0 * 1.3 * 0.9
    expected_high = (1 + BACKLINK_COEF * math.log1p(3)) * 1.0 * 1.3 * 0.9
    assert abs(by_id["high"]["score"] - expected_high) < 1e-9
    # mid: 1.0 * 0.5 * 1.0 * 0.9
    assert abs(by_id["mid"]["score"] - 0.45) < 1e-9
    assert [c["memory_id"] for c in ranked][:2] == ["high", "mid"]


@pytest.mark.asyncio
async def test_drift_lane_union_and_cosine_backfill():
    qdrant = MagicMock()
    drift_vec = _vec(0.7)
    point = MagicMock()
    point.id = "drift-only"
    point.vector = drift_vec
    qdrant.retrieve = MagicMock(return_value=[point])
    with (
        patch("genesis.qdrant.collections.search", side_effect=[[
            _hit("both-lanes", 0.8),
        ], []]),
        patch("genesis.memory.drift.drift_recall", new=AsyncMock(return_value=[
            _rr("both-lanes"),
            _rr("drift-only", confidence=1.0, mclass="fact"),
        ])),
        patch(
            "genesis.memory.retrieval._expired_candidate_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "genesis.db.crud.memory_links.batch_link_counts",
            new=AsyncMock(return_value={}),
        ),
    ):
        ranked = await rank_candidates(
            ema=EMA, entity_query="genesis voice", db=_db(),
            qdrant_client=qdrant, embedding_provider=MagicMock(),
        )
    by_id = {c["memory_id"]: c for c in ranked}
    assert by_id["both-lanes"]["lanes"] == ["vector", "drift"]
    # both-lanes keeps the VECTOR lane's payload (first lane wins)
    assert by_id["both-lanes"]["confidence"] == 0.8
    assert by_id["drift-only"]["lanes"] == ["drift"]
    assert abs(by_id["drift-only"]["cosine"] - 0.7) < 1e-6
    assert abs(by_id["drift-only"]["score"] - 1.0 * 1.0 * 0.7) < 1e-6
    # retrieve was called only for the drift-only candidate
    _, kwargs = qdrant.retrieve.call_args
    assert kwargs["ids"] == ["drift-only"]
    assert kwargs["with_vectors"] is True


@pytest.mark.asyncio
async def test_expired_candidates_dropped_and_empty_query_skips_drift():
    qdrant = MagicMock()
    qdrant.retrieve = MagicMock(return_value=[])
    drift_mock = AsyncMock(return_value=[])
    with (
        patch("genesis.qdrant.collections.search", side_effect=[[
            _hit("keep", 0.9), _hit("expired", 0.95),
        ], []]),
        patch("genesis.memory.drift.drift_recall", new=drift_mock),
        patch(
            "genesis.memory.retrieval._expired_candidate_ids",
            new=AsyncMock(return_value={"expired"}),
        ),
        patch(
            "genesis.db.crud.memory_links.batch_link_counts",
            new=AsyncMock(return_value={}),
        ),
    ):
        ranked = await rank_candidates(
            ema=EMA, entity_query="   ", db=_db(),
            qdrant_client=qdrant, embedding_provider=MagicMock(),
        )
    assert [c["memory_id"] for c in ranked] == ["keep"]
    drift_mock.assert_not_awaited()  # blank entity query → no drift lane


@pytest.mark.asyncio
async def test_missing_confidence_defaults_and_top_n_cap():
    qdrant = MagicMock()
    qdrant.retrieve = MagicMock(return_value=[])
    hits = [
        _hit(f"m{i}", 0.9 - i * 0.01, confidence=None, mclass=None)
        for i in range(TOP_N + 4)
    ]
    with (
        patch("genesis.qdrant.collections.search", side_effect=[hits, []]),
        patch("genesis.memory.drift.drift_recall", new=AsyncMock(return_value=[])),
        patch(
            "genesis.memory.retrieval._expired_candidate_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "genesis.db.crud.memory_links.batch_link_counts",
            new=AsyncMock(return_value={}),
        ),
    ):
        ranked = await rank_candidates(
            ema=EMA, entity_query="q", db=_db(),
            qdrant_client=qdrant, embedding_provider=MagicMock(),
        )
    assert len(ranked) == TOP_N
    assert ranked[0]["confidence"] == DEFAULT_CONFIDENCE
    assert abs(ranked[0]["score"] - DEFAULT_CONFIDENCE * 0.9) < 1e-9


@pytest.mark.asyncio
async def test_zero_writes_no_unexpected_client_calls():
    """The lane is read-only: search + retrieve are the ONLY qdrant calls,
    and nothing on the client that mutates (upsert/set_payload/update) runs."""
    qdrant = MagicMock()
    qdrant.retrieve = MagicMock(return_value=[])
    with (
        patch("genesis.qdrant.collections.search", side_effect=[[_hit("a", 0.9)], []]),
        patch("genesis.memory.drift.drift_recall", new=AsyncMock(return_value=[])),
        patch(
            "genesis.memory.retrieval._expired_candidate_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "genesis.db.crud.memory_links.batch_link_counts",
            new=AsyncMock(return_value={}),
        ),
    ):
        await rank_candidates(
            ema=EMA, entity_query="q", db=_db(),
            qdrant_client=qdrant, embedding_provider=MagicMock(),
        )
    for name in ("upsert", "set_payload", "overwrite_payload", "delete", "update_vectors"):
        assert not getattr(qdrant, name).called


@pytest.mark.asyncio
async def test_decision_lane_stratified_floor():
    """A low-scoring decision-lane candidate must survive the operational
    wall — the final set reserves slots for the decision class."""
    qdrant = MagicMock()
    qdrant.retrieve = MagicMock(return_value=[])
    ops_wall = [_hit(f"ops{i}", 0.9 - i * 0.001, confidence=1.0) for i in range(12)]
    decision = [_hit("the-decision", 0.73, confidence=0.9, mclass="fact")]
    with (
        patch(
            "genesis.qdrant.collections.search",
            side_effect=[ops_wall, decision],
        ) as search_mock,
        patch("genesis.memory.drift.drift_recall", new=AsyncMock(return_value=[])),
        patch(
            "genesis.memory.retrieval._expired_candidate_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "genesis.db.crud.memory_links.batch_link_counts",
            new=AsyncMock(return_value={}),
        ),
    ):
        ranked = await rank_candidates(
            ema=EMA, entity_query="q", db=_db(),
            qdrant_client=qdrant, embedding_provider=MagicMock(),
        )
    assert len(ranked) == TOP_N
    by_id = {c["memory_id"]: c for c in ranked}
    assert "the-decision" in by_id  # would be rank 13 by raw score
    assert by_id["the-decision"]["lanes"] == ["decision"]
    # Both lanes were exact, and only the decision lane was tag-filtered
    kw0 = search_mock.call_args_list[0].kwargs
    kw1 = search_mock.call_args_list[1].kwargs
    assert kw0["exact"] is True and kw0["tags_any"] is None
    assert kw1["exact"] is True and kw1["tags_any"] == ["decision"]


@pytest.mark.asyncio
async def test_decision_lane_dedup_with_vector_lane():
    """A candidate in both Qdrant lanes appears once, tagged with both."""
    qdrant = MagicMock()
    qdrant.retrieve = MagicMock(return_value=[])
    with (
        patch(
            "genesis.qdrant.collections.search",
            side_effect=[[_hit("dual", 0.85)], [_hit("dual", 0.85)]],
        ),
        patch("genesis.memory.drift.drift_recall", new=AsyncMock(return_value=[])),
        patch(
            "genesis.memory.retrieval._expired_candidate_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "genesis.db.crud.memory_links.batch_link_counts",
            new=AsyncMock(return_value={}),
        ),
    ):
        ranked = await rank_candidates(
            ema=EMA, entity_query="q", db=_db(),
            qdrant_client=qdrant, embedding_provider=MagicMock(),
        )
    assert len(ranked) == 1
    assert ranked[0]["lanes"] == ["vector", "decision"]


@pytest.mark.asyncio
async def test_drift_overfetch_recovers_precutoff_rows_under_as_of():
    """created_before replays post-filter drift rows; without over-fetch,
    top rows that are all post-cutoff would silently starve the lane
    (Codex P2, 2026-07-10). 4x fetch must refill to DRIFT_LANE_LIMIT."""
    from genesis.session_awareness.ranking import DRIFT_LANE_LIMIT

    cutoff = "2026-06-01T00:00:00+00:00"

    def _dated(mid, created):
        r = _rr(mid, confidence=1.0, mclass="fact")
        r.payload["created_at"] = created
        return r

    # The top 5 drift hits post-date the cutoff; 12 valid older rows sit
    # below them. Old behavior (limit=10, then filter) kept only 5.
    rows = [_dated(f"new-{i}", "2026-07-01T00:00:00+00:00") for i in range(5)]
    rows += [_dated(f"old-{i}", "2026-05-01T00:00:00+00:00") for i in range(12)]

    qdrant = MagicMock()
    qdrant.retrieve = MagicMock(return_value=[])
    drift_mock = AsyncMock(return_value=rows)
    with (
        patch("genesis.qdrant.collections.search", side_effect=[[], []]),
        patch("genesis.memory.drift.drift_recall", new=drift_mock),
        patch(
            "genesis.memory.retrieval._expired_candidate_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "genesis.db.crud.memory_links.batch_link_counts",
            new=AsyncMock(return_value={}),
        ),
    ):
        ranked = await rank_candidates(
            ema=EMA, entity_query="genesis voice", db=_db(),
            qdrant_client=qdrant, embedding_provider=MagicMock(),
            created_before=cutoff, entity_lane="off", top_n=30,
        )
    assert drift_mock.call_args.kwargs["limit"] == DRIFT_LANE_LIMIT * 4
    drift_ids = [c["memory_id"] for c in ranked if "drift" in c["lanes"]]
    assert len(drift_ids) == DRIFT_LANE_LIMIT  # lane refilled, not starved
    assert all(mid.startswith("old-") for mid in drift_ids)
