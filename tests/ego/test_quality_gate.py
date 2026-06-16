"""Tests for EgoSession._quality_gate — must fail OPEN on judge errors.

Regression guard for the Jun 2026 incident: the LLM quality judge's providers
were exhausted, the scorer returned ``(False, 0.0, {"error": "judge_call_fail"})``,
and the gate quarantined every proposal as ``quality_hold`` — silently halting
the entire proposal stream. A judge INFRASTRUCTURE failure is not a quality
failure; the gate's documented contract is to fail open.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


class _FakeScorer:
    """Stand-in scorer returning a fixed (passed, score, detail) tuple."""

    def __init__(self, result: tuple[bool, float, str]) -> None:
        self._result = result

    def set_router(self, router: object) -> None:
        pass

    async def score_async(self, **kwargs: object) -> tuple[bool, float, str]:
        return self._result


def _patch_scorer(scorer: _FakeScorer):
    return patch("genesis.eval.scorers.get_scorer", return_value=scorer)


def _session():
    from genesis.ego.session import EgoSession

    s = object.__new__(EgoSession)
    s._router = object()  # truthy → gate runs
    return s


def _detail(**fields: object) -> str:
    return json.dumps({"rubric_name": "output_quality", **fields})


@pytest.mark.asyncio
async def test_judge_call_fail_passes_through():
    """Providers exhausted (judge_call_fail) must NOT hold the proposal."""
    detail = _detail(error="judge_call_fail", error_message="All providers exhausted")
    proposals = [{"content": "Reprocess the DLQ", "rationale": "backlog"}]
    with _patch_scorer(_FakeScorer((False, 0.0, detail))):
        result = await _session()._quality_gate(proposals)
    assert result[0].get("_realist_verdict") != "quality_hold"


@pytest.mark.asyncio
async def test_judge_parse_fail_passes_through():
    """An unparseable judge response must NOT hold the proposal."""
    detail = _detail(error="judge_parse_fail", error_message="bad json")
    proposals = [{"content": "Ship the brief"}]
    with _patch_scorer(_FakeScorer((False, 0.0, detail))):
        result = await _session()._quality_gate(proposals)
    assert result[0].get("_realist_verdict") != "quality_hold"


@pytest.mark.asyncio
async def test_genuine_low_score_is_held():
    """A real below-threshold score (no judge error) still holds."""
    detail = _detail(judge_score=0.2, rationale="incoherent")
    proposals = [{"content": "garbage"}]
    with _patch_scorer(_FakeScorer((False, 0.2, detail))):
        result = await _session()._quality_gate(proposals)
    assert result[0].get("_realist_verdict") == "quality_hold"


@pytest.mark.asyncio
async def test_passing_score_not_held():
    """A passing score leaves the proposal unannotated."""
    detail = _detail(judge_score=0.9, rationale="solid")
    proposals = [{"content": "good"}]
    with _patch_scorer(_FakeScorer((True, 0.9, detail))):
        result = await _session()._quality_gate(proposals)
    assert "_realist_verdict" not in result[0]
