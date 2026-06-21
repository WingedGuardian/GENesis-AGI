"""Tests for the reflection-prompt A/B runner (stubbed gen + judge — no API).

Proves the runner's score aggregation, error handling, limit, validation, and
that control-vs-treatment is decided on the continuous judge score.
"""

import json
from types import SimpleNamespace

import pytest

from genesis.experimentation.runner import _extract_reflection_text, run_reflection_experiment
from genesis.experimentation.types import CognitiveVariant


class _StubGenRouter:
    """Returns a 'good' reflection when the system prompt contains REAL, else weak."""

    async def route_call(self, call_site_id, messages, **kwargs):
        system = messages[0]["content"]
        if "REAL" in system:
            content = '{"observations": ["a specific grounded actionable insight about the scheduler"]}'
        else:
            content = '{"observations": ["vague thing"]}'
        return SimpleNamespace(success=True, content=content, error=None)

    async def close(self):
        pass


class _FailGenRouter:
    async def route_call(self, call_site_id, messages, **kwargs):
        return SimpleNamespace(success=False, content=None, error="boom")

    async def close(self):
        pass


class _StubJudge:
    """High score for the 'specific' (control) output, low for the weak one."""

    async def score_async(self, actual, expected, config):
        if "specific" in actual:
            return True, 0.8, "{}"
        return False, 0.1, "{}"


def _write_golden(tmp_path, n):
    path = tmp_path / "golden.jsonl"
    lines = [
        json.dumps(
            {
                "id": f"case_{i}",
                "actual": "original reflection text",
                "user_passed": True,
                "expected": "deep_reflection_observation",
                "scorer_config": {
                    "rubric_name": "reflection_quality",
                    "session_context": f"signals for tick {i}",
                },
            }
        )
        for i in range(n)
    ]
    path.write_text("\n".join(lines))
    return path


def _variants():
    return (
        CognitiveVariant(name="real", system_prompt="REAL deep reflection prompt"),
        CognitiveVariant(name="weak", system_prompt="weak prompt"),
    )


async def test_control_wins_on_score(tmp_path):
    golden = _write_golden(tmp_path, 6)
    control, treatment = _variants()
    res = await run_reflection_experiment(
        experiment_name="t",
        control=control,
        treatment=treatment,
        golden_set_path=golden,
        gen_router=_StubGenRouter(),
        judge=_StubJudge(),
    )
    assert res.errors == 0
    assert res.control.mean_score == pytest.approx(0.8)
    assert res.treatment.mean_score == pytest.approx(0.1)
    assert res.winrate["n_control_wins"] == 6
    assert res.winrate["significant"] is True
    assert res.winrate["recommendation"] == "control_wins"
    # binary pass/fail context agrees (control passes, treatment fails every case)
    assert res.control.n_pass == 6
    assert res.treatment.n_pass == 0
    assert res.metadata["pass_winrate"]["recommendation"] == "control_wins"


async def test_generation_failure_degrades_to_insufficient(tmp_path):
    golden = _write_golden(tmp_path, 4)
    control, treatment = _variants()
    res = await run_reflection_experiment(
        experiment_name="t",
        control=control,
        treatment=treatment,
        golden_set_path=golden,
        gen_router=_FailGenRouter(),
        judge=_StubJudge(),
    )
    assert res.errors == 8  # 4 cases x 2 arms, all generation failures
    assert res.control.mean_score == 0.0
    assert res.winrate["recommendation"] == "insufficient_data"


async def test_limit_truncates_cases(tmp_path):
    golden = _write_golden(tmp_path, 10)
    control, treatment = _variants()
    res = await run_reflection_experiment(
        experiment_name="t",
        control=control,
        treatment=treatment,
        golden_set_path=golden,
        limit=3,
        gen_router=_StubGenRouter(),
        judge=_StubJudge(),
    )
    assert res.n_cases == 3
    assert len(res.control.case_scores) == 3


async def test_missing_system_prompt_raises(tmp_path):
    golden = _write_golden(tmp_path, 2)
    with pytest.raises(ValueError, match="system_prompt"):
        await run_reflection_experiment(
            experiment_name="t",
            control=CognitiveVariant(name="a", system_prompt=None),
            treatment=CognitiveVariant(name="b", system_prompt="weak"),
            golden_set_path=golden,
            gen_router=_StubGenRouter(),
            judge=_StubJudge(),
        )


def test_extract_reflection_text():
    assert _extract_reflection_text('{"observations": ["a", "b"]}') == "a\nb"
    assert _extract_reflection_text('prose {"observations": ["x"]} tail') == "x"
    assert _extract_reflection_text('{"cognitive_state_update": "state"}') == "state"
    assert _extract_reflection_text("not json at all") == "not json at all"
    assert _extract_reflection_text("") == ""
