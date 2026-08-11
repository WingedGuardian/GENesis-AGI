"""MW-2 lean keystone — the reusable relationship-classifier FUNCTION.

Given two memory contents, classify their relationship into a COARSE, tractable
vocabulary (``duplicate`` / ``contradicts`` / ``succeeded_by`` / ``distinct``)
with a confidence. This is the judgment MW-5's merge gate consumes. Generalizes
``entity_resolution.check_semantic_overlap`` (which does duplicate/contradicts/
distinct) by adding ``succeeded_by`` (old→new truth) + a confidence field +
few-shot prompting. Fine-grained typing (extends/supports/elaborates) is
DELIBERATELY not attempted — it is literature-unreliable on already-similar pairs,
and no consumer reads it (recall boost is type-agnostic).

Fail-safe posture (mirrors the precedent): any LLM/parse failure, an out-of-vocab
verdict, or a low/absent confidence resolves to ``distinct`` at confidence 0.0 —
never fabricate a merge-eligible verdict on uncertainty.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.memory.relationship_classifier import (
    COARSE_RELATIONSHIPS,
    classify_relationship,
    classify_relationships,
)


def _router(*contents, success=True, error=None):
    """AsyncMock router whose route_call returns queued MagicMock results."""
    router = AsyncMock()
    results = [MagicMock(success=success, content=c, error=error) for c in contents]
    router.route_call.side_effect = results if len(results) > 1 else None
    if len(results) == 1:
        router.route_call.return_value = results[0]
    return router


# ── vocabulary contract ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_coarse_vocab_is_the_four_tractable_types():
    assert frozenset(
        {"duplicate", "contradicts", "succeeded_by", "distinct"}
    ) == COARSE_RELATIONSHIPS
    # The fine-grained types the OLD linker fabricated must NOT be emittable.
    for fine in ("extends", "supports", "elaborates", "related_to"):
        assert fine not in COARSE_RELATIONSHIPS


# ── single-pair classification ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_parses_valid_contradicts_verdict():
    router = _router(
        json.dumps(
            {"relationship": "contradicts", "confidence": 0.9, "reasoning": "opposite numbers"}
        )
    )
    v = await classify_relationship(router, "revenue up 10%", "revenue down 10%")
    assert v["relationship"] == "contradicts"
    assert v["confidence"] == 0.9
    assert v["reasoning"]


@pytest.mark.asyncio
async def test_parses_succeeded_by_with_fence_stripped():
    router = _router('```json\n{"relationship":"succeeded_by","confidence":0.7}\n```')
    v = await classify_relationship(router, "old truth", "new truth", newer="b")
    assert v["relationship"] == "succeeded_by"
    assert v["confidence"] == 0.7


@pytest.mark.asyncio
async def test_llm_failure_defaults_distinct_zero_conf():
    router = _router(None, success=False, error="timeout")
    v = await classify_relationship(router, "a", "b")
    assert v["relationship"] == "distinct"
    assert v["confidence"] == 0.0


@pytest.mark.asyncio
async def test_bad_json_defaults_distinct_zero_conf():
    router = _router("not json at all")
    v = await classify_relationship(router, "a", "b")
    assert v["relationship"] == "distinct"
    assert v["confidence"] == 0.0


@pytest.mark.asyncio
async def test_out_of_vocab_verdict_coerced_to_distinct():
    # 'extends' is deliberately excluded — an LLM that emits it must NOT leak a
    # fine-grained (unreliable) type through as truth.
    router = _router(json.dumps({"relationship": "extends", "confidence": 0.85}))
    v = await classify_relationship(router, "a", "b")
    assert v["relationship"] == "distinct"
    assert v["confidence"] == 0.0


@pytest.mark.asyncio
async def test_confidence_clamped_to_unit_interval():
    router = _router(json.dumps({"relationship": "duplicate", "confidence": 1.7}))
    v = await classify_relationship(router, "a", "b")
    assert v["relationship"] == "duplicate"
    assert v["confidence"] == 1.0


@pytest.mark.asyncio
async def test_missing_confidence_defaults_low_not_high():
    # A verdict with no confidence must not be trusted as high-confidence.
    router = _router(json.dumps({"relationship": "duplicate"}))
    v = await classify_relationship(router, "a", "b")
    assert v["relationship"] == "duplicate"
    assert v["confidence"] == 0.0


@pytest.mark.asyncio
async def test_uses_the_dedicated_call_site_id():
    router = _router(json.dumps({"relationship": "distinct", "confidence": 0.5}))
    await classify_relationship(router, "a", "b")
    call_site = router.route_call.await_args.args[0]
    assert call_site == "dream_cycle_relationship_classify"


# ── batched classification ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_returns_verdict_per_pair_aligned_by_index():
    router = _router(
        json.dumps(
            [
                {"pair_id": 0, "relationship": "duplicate", "confidence": 0.9},
                {"pair_id": 1, "relationship": "distinct", "confidence": 0.6},
            ]
        )
    )
    pairs = [("a0", "b0"), ("a1", "b1")]
    verdicts = await classify_relationships(router, pairs)
    assert len(verdicts) == 2
    assert verdicts[0]["relationship"] == "duplicate"
    assert verdicts[1]["relationship"] == "distinct"


@pytest.mark.asyncio
async def test_batch_bad_json_all_default_distinct():
    router = _router("garbage not json")
    pairs = [("a0", "b0"), ("a1", "b1")]
    verdicts = await classify_relationships(router, pairs)
    assert len(verdicts) == 2
    assert all(v["relationship"] == "distinct" and v["confidence"] == 0.0 for v in verdicts)


@pytest.mark.asyncio
async def test_batch_missing_pair_defaults_distinct():
    # LLM only returned a verdict for pair 0 — pair 1 must fail safe.
    router = _router(json.dumps([{"pair_id": 0, "relationship": "contradicts", "confidence": 0.8}]))
    pairs = [("a0", "b0"), ("a1", "b1")]
    verdicts = await classify_relationships(router, pairs)
    assert verdicts[0]["relationship"] == "contradicts"
    assert verdicts[1]["relationship"] == "distinct"
    assert verdicts[1]["confidence"] == 0.0


@pytest.mark.asyncio
async def test_batch_empty_pairs_returns_empty():
    router = _router(json.dumps([]))
    verdicts = await classify_relationships(router, [])
    assert verdicts == []
    # No LLM call for an empty batch.
    router.route_call.assert_not_called()
