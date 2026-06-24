"""procedure_recall re-ranks surfaced procedures by reads (effective confidence).

Read-ranking is isolated to the recall path — `find_relevant`'s global behavior
is unchanged (it has 6 callers incl. autonomy outcome-attribution).
"""

from __future__ import annotations

import pytest

from genesis.db.crud import procedural
from genesis.learning.procedural.matcher import find_relevant
from genesis.mcp.memory.procedural import _rank_by_effective_confidence


def test_rank_orders_read_heavy_first():
    results = [
        # eff_conf = 2/3
        {"procedure_id": "low-read", "success_count": 1, "failure_count": 0,
         "invocation_count": 0},
        # 30 reads → 6 eff successes → eff_conf = 7/8
        {"procedure_id": "high-read", "success_count": 0, "failure_count": 0,
         "invocation_count": 30},
    ]
    ranked = _rank_by_effective_confidence(results)
    assert [r["procedure_id"] for r in ranked] == ["high-read", "low-read"]


def test_rank_preserves_relevance_order_on_ties():
    # Equal effective confidence → keep find_relevant's input (relevance) order.
    results = [
        {"procedure_id": "a", "success_count": 1, "failure_count": 0,
         "invocation_count": 0},
        {"procedure_id": "b", "success_count": 1, "failure_count": 0,
         "invocation_count": 0},
    ]
    ranked = _rank_by_effective_confidence(results)
    assert [r["procedure_id"] for r in ranked] == ["a", "b"]


@pytest.mark.asyncio
async def test_find_relevant_carries_invocation_count(db):
    """ProcedureMatch surfaces invocation_count so recall can rank on it."""
    await procedural.create(
        db, id="m1", task_type="t", principle="p", steps=["s"],
        tools_used=[], context_tags=["alpha"],
        created_at="2026-01-01T00:00:00",
        confidence=0.6, speculative=0, activation_tier="LIBRARY",
    )
    await procedural.record_invocation(db, "m1")
    matches = await find_relevant(db, ["alpha"], min_confidence=0.3, limit=5)
    assert matches
    assert matches[0].procedure_id == "m1"
    assert matches[0].invocation_count == 1
