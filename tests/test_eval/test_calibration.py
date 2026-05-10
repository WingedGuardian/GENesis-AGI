"""Tests for rubric calibration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from genesis.eval.calibration import (
    DEFAULT_AGREEMENT_THRESHOLD,
    _load_golden_set,
    render_report,
    run_calibration,
)
from genesis.eval.rubrics import _RUBRICS, Rubric, register_rubric
from genesis.routing.types import RoutingResult


@dataclass
class ProgrammableRouter:
    """Router stub that returns scripted responses keyed by call order."""

    responses: list[str]
    _call_count: int = 0

    async def route_call(self, call_site_id, messages, **kwargs):
        if self._call_count >= len(self.responses):
            content = self.responses[-1]  # repeat last response
        else:
            content = self.responses[self._call_count]
        self._call_count += 1
        return RoutingResult(
            success=True,
            call_site_id=call_site_id,
            content=content,
            model_id="test-model",
            provider_used="test-provider",
        )


@pytest.fixture
def fresh_registry():
    snapshot = dict(_RUBRICS)
    yield
    _RUBRICS.clear()
    _RUBRICS.update(snapshot)


@pytest.fixture
def calibration_rubric(fresh_registry):
    r = Rubric(
        name="cal_test",
        version="1.0.0",
        description="test",
        prompt_template="a={actual} e={expected}",
        pass_threshold=0.5,
    )
    register_rubric(r)
    return r


def _write_golden(tmp_path: Path, cases: list[dict]) -> Path:
    p = tmp_path / "golden.jsonl"
    p.write_text("\n".join(json.dumps(c) for c in cases))
    return p


def test_load_golden_set_skips_comments_and_blanks(tmp_path):
    p = tmp_path / "golden.jsonl"
    p.write_text(
        "# leading comment\n"
        "\n"
        '{"id": "c1", "actual": "x", "user_passed": true}\n'
        "  \n"
        '# trailing comment\n'
        '{"id": "c2", "actual": "y", "user_passed": false}\n',
    )
    cases = _load_golden_set(p)
    assert [c["id"] for c in cases] == ["c1", "c2"]


def test_load_golden_set_missing_required_field_raises(tmp_path):
    p = tmp_path / "golden.jsonl"
    p.write_text('{"id": "c1", "actual": "x"}\n')  # missing user_passed
    with pytest.raises(ValueError, match="user_passed"):
        _load_golden_set(p)


def test_load_golden_set_invalid_json_raises(tmp_path):
    p = tmp_path / "golden.jsonl"
    p.write_text('{"id": "c1", actual: "x"}\n')  # bad JSON
    with pytest.raises(ValueError, match="invalid JSON"):
        _load_golden_set(p)


def test_load_golden_set_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        _load_golden_set(tmp_path / "nope.jsonl")


async def test_run_calibration_full_agreement(calibration_rubric, tmp_path):
    """All judge decisions match user — agreement = 1.0."""
    cases = [
        {"id": "c1", "actual": "x", "user_passed": True},
        {"id": "c2", "actual": "y", "user_passed": False},
        {"id": "c3", "actual": "z", "user_passed": True},
    ]
    golden = _write_golden(tmp_path, cases)
    router = ProgrammableRouter(responses=[
        '{"score": 0.9, "rationale": ""}',  # judge passes
        '{"score": 0.1, "rationale": ""}',  # judge fails
        '{"score": 0.7, "rationale": ""}',  # judge passes
    ])

    result = await run_calibration(
        rubric="cal_test",
        golden_set_path=golden,
        router=router,
    )
    assert result.total_cases == 3
    assert result.agreed_cases == 3
    assert result.disagreed_cases == 0
    assert result.error_cases == 0
    assert result.agreement_rate == 1.0
    assert result.threshold_met is True


async def test_run_calibration_partial_agreement(
    calibration_rubric, tmp_path,
):
    """Below threshold → threshold_met False."""
    cases = [
        {"id": "c1", "actual": "x", "user_passed": True},
        {"id": "c2", "actual": "y", "user_passed": True},
        {"id": "c3", "actual": "z", "user_passed": True},
    ]
    golden = _write_golden(tmp_path, cases)
    router = ProgrammableRouter(responses=[
        '{"score": 0.9, "rationale": ""}',  # passes — agree
        '{"score": 0.1, "rationale": ""}',  # fails — disagree
        '{"score": 0.1, "rationale": ""}',  # fails — disagree
    ])

    result = await run_calibration(
        rubric="cal_test",
        golden_set_path=golden,
        router=router,
    )
    assert result.agreed_cases == 1
    assert result.disagreed_cases == 2
    assert result.agreement_rate == pytest.approx(1 / 3)
    assert result.threshold_met is False


async def test_run_calibration_errors_count_as_disagreement(
    calibration_rubric, tmp_path,
):
    """Judge parse failure = error case, not agreement."""
    cases = [
        {"id": "c1", "actual": "x", "user_passed": True},
    ]
    golden = _write_golden(tmp_path, cases)
    router = ProgrammableRouter(responses=["nonsense not json"])

    result = await run_calibration(
        rubric="cal_test",
        golden_set_path=golden,
        router=router,
    )
    assert result.error_cases == 1
    assert result.agreed_cases == 0
    assert result.threshold_met is False


async def test_run_calibration_empty_set_raises(
    calibration_rubric, tmp_path,
):
    p = tmp_path / "empty.jsonl"
    p.write_text("# only comments\n")
    with pytest.raises(ValueError, match="no graded cases"):
        await run_calibration(
            rubric="cal_test",
            golden_set_path=p,
            router=ProgrammableRouter(responses=[]),
        )


async def test_run_calibration_default_threshold_is_80_percent(
    calibration_rubric, tmp_path,
):
    """The 80% ship gate must be the default — guard against silent
    drift if anyone bumps it without updating tests + docs."""
    assert DEFAULT_AGREEMENT_THRESHOLD == 0.80
    cases = [
        {"id": str(i), "actual": "x", "user_passed": True}
        for i in range(10)
    ]
    golden = _write_golden(tmp_path, cases)
    # 8/10 agree → exactly at threshold → threshold_met True
    router = ProgrammableRouter(responses=(
        ['{"score": 0.9, "rationale": ""}'] * 8
        + ['{"score": 0.1, "rationale": ""}'] * 2
    ))
    result = await run_calibration(
        rubric="cal_test",
        golden_set_path=golden,
        router=router,
    )
    assert result.agreement_rate == 0.8
    assert result.threshold_met is True


def test_render_report_promote_verdict():
    from genesis.eval.calibration import (
        CalibrationCaseOutcome,
        CalibrationResult,
    )
    result = CalibrationResult(
        rubric_name="r", rubric_version="1.0.0", judge_call_site="judge",
        total_cases=10, agreed_cases=9, disagreed_cases=1, error_cases=0,
        agreement_rate=0.9, threshold=0.8, threshold_met=True,
        duration_s=12.5, generated_at="2026-05-10T00:00:00+00:00",
        outcomes=[CalibrationCaseOutcome(
            case_id="c1", user_passed=True, judge_passed=False,
            judge_score=0.4, agreed=False, rationale="missed nuance",
        )],
    )
    md = render_report(result)
    assert "**Verdict:** PROMOTE" in md
    assert "90.0% agreement" in md
    # Disagreement is surfaced
    assert "c1" in md
    assert "missed nuance" in md


def test_render_report_blocked_verdict():
    from genesis.eval.calibration import CalibrationResult
    result = CalibrationResult(
        rubric_name="r", rubric_version="1.0.0", judge_call_site="judge",
        total_cases=10, agreed_cases=5, disagreed_cases=5, error_cases=0,
        agreement_rate=0.5, threshold=0.8, threshold_met=False,
        duration_s=12.5, generated_at="2026-05-10T00:00:00+00:00",
    )
    md = render_report(result)
    assert "**Verdict:** BLOCKED" in md
    assert "do NOT promote" in md
