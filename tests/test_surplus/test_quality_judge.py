"""Tests for the measurement-only surplus quality judge (surplus.quality_judge).

The judge grades a completed insight's FULL output with the eval LLM-judge and
maps the verdict onto ``outcome_quality`` (useful/hollow/NULL). These tests pin
the mapping and — critically — the OUTAGE GUARD: a judge-provider failure returns
``passed=False`` with an ``"error"`` key, which must become a NULL verdict, never
a false ``hollow`` (which would flood the Outcome Bus with fake negatives).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.surplus.quality_judge import _JUDGE_EXPECTED, run_quality_judge
from genesis.surplus.types import INSIGHT_PRODUCING_TASK_TYPES, TaskType

pytestmark = pytest.mark.asyncio


def _patch_scorer(passed: bool, score: float, detail: str):
    """Patch OutputQualityScorer so score_async returns a fixed verdict."""
    scorer = MagicMock()
    scorer.score_async = AsyncMock(return_value=(passed, score, detail))
    cls = MagicMock(return_value=scorer)
    return patch("genesis.eval.scorers.OutputQualityScorer", cls)


async def test_expected_keys_exactly_cover_insight_types():
    # Invariant guard: every insight-producing type has an expected string and
    # there are no extras. If someone adds a type to INSIGHT_PRODUCING_TASK_TYPES
    # without an expected string, that type would silently get NULL verdicts.
    assert set(_JUDGE_EXPECTED) == set(INSIGHT_PRODUCING_TASK_TYPES)


async def test_pass_maps_to_useful():
    detail = json.dumps({"judge_score": 0.82, "rationale": "solid, on-topic"})
    with _patch_scorer(True, 0.82, detail):
        oq, score, d = await run_quality_judge(
            "a substantive real insight body ...", TaskType.BRAINSTORM_USER, object(),
        )
    assert oq == "useful"
    assert score == 0.82
    assert d == detail


async def test_fail_maps_to_hollow():
    detail = json.dumps({"judge_score": 0.30, "rationale": "vague filler"})
    with _patch_scorer(False, 0.30, detail):
        oq, score, d = await run_quality_judge(
            "weak body", TaskType.CODE_AUDIT, object(),
        )
    assert oq == "hollow"
    assert score == 0.30
    assert d == detail


async def test_call_fail_error_key_maps_to_null():
    # Provider outage: score_async returns passed=False + an "error" key. This
    # MUST become NULL, not hollow (otherwise an outage floods false negatives).
    detail = json.dumps({"error": "judge_call_fail", "error_message": "boom"})
    with _patch_scorer(False, 0.0, detail):
        oq, score, d = await run_quality_judge(
            "body", TaskType.BRAINSTORM_USER, object(),
        )
    assert (oq, score, d) == (None, None, None)


async def test_parse_fail_error_key_maps_to_null():
    detail = json.dumps({"error": "judge_parse_fail", "error_message": "bad json"})
    with _patch_scorer(False, 0.0, detail):
        oq, _, _ = await run_quality_judge("body", TaskType.WING_AUDIT, object())
    assert oq is None


async def test_scorer_raises_maps_to_null():
    scorer = MagicMock()
    scorer.score_async = AsyncMock(side_effect=RuntimeError("kaboom"))
    with patch(
        "genesis.eval.scorers.OutputQualityScorer", MagicMock(return_value=scorer),
    ):
        oq, score, d = await run_quality_judge(
            "body", TaskType.BRAINSTORM_USER, object(),
        )
    assert (oq, score, d) == (None, None, None)


async def test_no_router_returns_null_without_constructing_scorer():
    with patch("genesis.eval.scorers.OutputQualityScorer") as cls:
        oq, score, d = await run_quality_judge(
            "body", TaskType.BRAINSTORM_USER, None,
        )
    assert (oq, score, d) == (None, None, None)
    cls.assert_not_called()


async def test_non_insight_type_returns_null_without_constructing_scorer():
    # A type absent from _JUDGE_EXPECTED (e.g. an action/index type) short-circuits
    # BEFORE the judge call — no LLM cost, no verdict.
    with patch("genesis.eval.scorers.OutputQualityScorer") as cls:
        oq, _, _ = await run_quality_judge("body", TaskType.CODE_INDEX, object())
    assert oq is None
    cls.assert_not_called()


async def test_accepts_string_task_type():
    # StrEnum: callers may pass the raw value; it must resolve to the enum.
    detail = json.dumps({"judge_score": 0.9})
    with _patch_scorer(True, 0.9, detail):
        oq, _, _ = await run_quality_judge("body", "brainstorm_user", object())
    assert oq == "useful"


async def test_unparseable_detail_without_error_key_still_useful():
    # Defensive: if the judge returns a real pass but detail isn't JSON we can
    # parse, absence of an "error" key means it is NOT an outage → keep the verdict.
    with _patch_scorer(True, 0.75, "not-json-detail"):
        oq, score, _ = await run_quality_judge(
            "body", TaskType.ANTICIPATORY_RESEARCH, object(),
        )
    assert oq == "useful"
    assert score == 0.75
