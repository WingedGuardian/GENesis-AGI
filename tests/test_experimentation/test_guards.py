"""Tests for the judge-hack guards."""

import json
from types import SimpleNamespace

from genesis.experimentation.guards import (
    check_rubric_calibrated,
    held_out_verdict,
    pin_rubric_version,
)


def test_pin_rubric_version_returns_version():
    v = pin_rubric_version("reflection_quality")
    assert isinstance(v, str) and v  # e.g. "1.0.0"


def test_held_out_survives_when_judges_agree():
    out = held_out_verdict("treatment_wins", "treatment_wins")
    assert out["survives"] is True
    assert out["flag"] is None


def test_held_out_flags_judge_overfit_on_disagreement():
    out = held_out_verdict("treatment_wins", "no_difference")
    assert out["survives"] is False
    assert out["flag"] == "judge_overfit"


def test_held_out_non_directional_has_nothing_to_validate():
    out = held_out_verdict("insufficient_data", "control_wins")
    assert out["survives"] is True
    assert out["flag"] is None


class _StubJudgeRouter:
    """Returns a fixed judge score so every golden case 'passes' the judge."""

    def __init__(self, score=0.8):
        self._score = score

    async def route_call(self, call_site_id, messages, **kwargs):
        return SimpleNamespace(
            success=True,
            content=json.dumps({"score": self._score, "rationale": "ok"}),
            error=None,
            model_id="stub-judge",
            provider_used="stub",
        )

    async def close(self):
        pass


def _write_golden(tmp_path, n, *, user_passed):
    path = tmp_path / "golden.jsonl"
    lines = [
        json.dumps({
            "id": f"c{i}",
            "actual": "some reflection text",
            "user_passed": user_passed,
            "expected": "deep_reflection_observation",
            "scorer_config": {"rubric_name": "reflection_quality", "session_context": "sig"},
        })
        for i in range(n)
    ]
    path.write_text("\n".join(lines))
    return path


async def test_check_rubric_calibrated_passes(tmp_path):
    # judge passes all (0.8 >= 0.6) and golden labels all pass -> 100% agreement
    golden = _write_golden(tmp_path, 5, user_passed=True)
    out = await check_rubric_calibrated(
        "reflection_quality", golden, router=_StubJudgeRouter(score=0.8),
    )
    assert out["calibrated"] is True
    assert out["agreement_rate"] == 1.0
    assert out["n_cases"] == 5
    assert out["rubric_version"]


async def test_check_rubric_calibrated_fails_on_disagreement(tmp_path):
    # judge passes all, golden labels all fail -> 0% agreement -> not calibrated
    golden = _write_golden(tmp_path, 5, user_passed=False)
    out = await check_rubric_calibrated(
        "reflection_quality", golden, router=_StubJudgeRouter(score=0.8),
    )
    assert out["calibrated"] is False
    assert out["agreement_rate"] == 0.0
