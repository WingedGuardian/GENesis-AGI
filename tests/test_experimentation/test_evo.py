"""Tests for the Evo orchestration core (recommend-only, measure-only).

Hermetic: `run_reflection_experiment` is mocked to return controlled win-rates
per candidate, so these exercise the gating / selection / held-out-revalidation
logic (the winner's-curse defense) without live model calls.
"""

import json
from pathlib import Path

import pytest

from genesis.experimentation.evo import EvoConfig, run_evo
from genesis.experimentation.types import ArmResult, CognitiveVariant, ExperimentResult


def _golden(tmp_path: Path, n: int = 8) -> Path:
    p = tmp_path / "golden.jsonl"
    lines = [
        json.dumps({
            "id": f"c{i}", "actual": "x", "user_passed": True,
            "scorer_config": {"session_context": "ctx", "rubric_name": "reflection_quality"},
        })
        for i in range(n)
    ]
    p.write_text("\n".join(lines) + "\n")
    return p


def _result(treatment_name, *, rec, p_value, mean_score, significant=True):
    return ExperimentResult(
        experiment_name="x",
        control=ArmResult(variant_name="base", case_scores=[0.5], case_results=[True], n_pass=1, mean_score=0.5),
        treatment=ArmResult(variant_name=treatment_name, case_scores=[mean_score], case_results=[True], n_pass=1, mean_score=mean_score),
        winrate={"recommendation": rec, "p_value": p_value, "significant": significant},
        n_cases=4,
        errors=0,
        metadata={},
    )


def _base():
    return CognitiveVariant(name="base", system_prompt="BASE")


def _cands(*names):
    return [CognitiveVariant(name=n, system_prompt=f"VARIANT {n}") for n in names]


@pytest.fixture
def patch_run(monkeypatch):
    """Patch run_reflection_experiment with a dict: treatment_name -> ExperimentResult.

    Distinguishes the held-out re-validation pass by experiment_name prefix
    ('evo:holdout:') so a test can make a candidate win the fan-out but fail
    (or pass) the held-out slice.
    """
    table: dict = {}

    async def fake(**kwargs):
        name = kwargs["treatment"].name
        key = ("holdout:" + name) if kwargs["experiment_name"].startswith("evo:holdout:") else name
        return table[key]

    monkeypatch.setattr("genesis.experimentation.runner.run_reflection_experiment", fake)
    return table


async def test_only_bonferroni_significant_treatment_wins_survive(tmp_path, patch_run):
    # 4 candidates → Bonferroni threshold = 0.05/4 = 0.0125
    base = _base()
    cands = _cands("a", "b", "c", "d")
    patch_run["a"] = _result("a", rec="treatment_wins", p_value=0.01, mean_score=0.7)   # survives
    patch_run["b"] = _result("b", rec="treatment_wins", p_value=0.03, mean_score=0.9)   # p>0.0125 → out
    patch_run["c"] = _result("c", rec="control_wins", p_value=0.001, mean_score=0.2)    # wrong dir → out
    patch_run["d"] = _result("d", rec="no_difference", p_value=0.5, mean_score=0.6)     # out
    # 'a' is the only survivor; held-out confirms it
    patch_run["holdout:a"] = _result("a", rec="treatment_wins", p_value=0.01, mean_score=0.72)

    out = await run_evo(base=base, candidates=cands, golden_set_path=_golden(tmp_path),
                        config=EvoConfig(eval_limit=4, holdout_limit=4))
    assert out.candidates_evaluated == 4
    assert out.survivors == 1
    assert out.winner is not None and out.winner.name == "a"


async def test_best_survivor_by_mean_score_then_holdout_confirms(tmp_path, patch_run):
    base = _base()
    cands = _cands("a", "b")
    # both survive the gate; b has higher mean_score → chosen
    patch_run["a"] = _result("a", rec="treatment_wins", p_value=0.001, mean_score=0.7)
    patch_run["b"] = _result("b", rec="treatment_wins", p_value=0.001, mean_score=0.85)
    patch_run["holdout:b"] = _result("b", rec="treatment_wins", p_value=0.005, mean_score=0.83)

    out = await run_evo(base=base, candidates=cands, golden_set_path=_golden(tmp_path),
                        config=EvoConfig(eval_limit=4, holdout_limit=4))
    assert out.winner is not None and out.winner.name == "b"


async def test_winner_rejected_when_holdout_fails(tmp_path, patch_run):
    """Winner's-curse defense: a fan-out winner that does NOT survive the
    held-out re-validation is rejected (winner=None)."""
    base = _base()
    cands = _cands("a")
    patch_run["a"] = _result("a", rec="treatment_wins", p_value=0.001, mean_score=0.9)
    patch_run["holdout:a"] = _result("a", rec="no_difference", p_value=0.4, mean_score=0.55, significant=False)

    out = await run_evo(base=base, candidates=cands, golden_set_path=_golden(tmp_path),
                        config=EvoConfig(eval_limit=4, holdout_limit=4))
    assert out.survivors == 1
    assert out.winner is None  # held-out did not confirm


async def test_no_survivors_returns_no_winner(tmp_path, patch_run):
    base = _base()
    cands = _cands("a", "b")
    patch_run["a"] = _result("a", rec="no_difference", p_value=0.6, mean_score=0.5)
    patch_run["b"] = _result("b", rec="control_wins", p_value=0.01, mean_score=0.3)

    out = await run_evo(base=base, candidates=cands, golden_set_path=_golden(tmp_path),
                        config=EvoConfig(eval_limit=4, holdout_limit=4))
    assert out.survivors == 0
    assert out.winner is None


async def test_empty_candidates_is_safe(tmp_path, patch_run):
    out = await run_evo(base=_base(), candidates=[], golden_set_path=_golden(tmp_path),
                        config=EvoConfig(eval_limit=4, holdout_limit=4))
    assert out.winner is None
    assert out.candidates_evaluated == 0


# --- build_directive_variants (deterministic directive-append) ---


def test_build_directive_variants_appends_directives():
    from genesis.experimentation.evo import build_directive_variants

    out = build_directive_variants("BASE PROMPT", 3)
    assert len(out) == 3
    assert all(v.name == f"evo_v{i}" for i, v in enumerate(out))
    # each variant = base + a distinct directive (no truncation, deterministic)
    assert all(v.system_prompt.startswith("BASE PROMPT\n\n") for v in out)
    assert len({v.system_prompt for v in out}) == 3  # distinct directives
    assert all(v.description for v in out)


def test_build_directive_variants_caps_at_directive_count():
    from genesis.experimentation.evo import build_directive_variants

    out = build_directive_variants("BASE", 99)
    assert len(out) == 6  # capped at the number of fixed directives
    out0 = build_directive_variants("BASE", 0)
    assert out0 == []


async def test_holdout_disjoint_false_on_small_golden_set(tmp_path, patch_run):
    # golden set of 4, eval_limit=4 → nothing left to hold out → not disjoint
    base = _base()
    cands = _cands("a")
    patch_run["a"] = _result("a", rec="treatment_wins", p_value=0.001, mean_score=0.9)
    patch_run["holdout:a"] = _result("a", rec="treatment_wins", p_value=0.01, mean_score=0.9)

    out = await run_evo(base=base, candidates=cands, golden_set_path=_golden(tmp_path, n=4),
                        config=EvoConfig(eval_limit=4, holdout_limit=4))
    assert out.holdout_disjoint is False  # surfaced, not hidden


async def test_holdout_disjoint_true_on_large_golden_set(tmp_path, patch_run):
    base = _base()
    cands = _cands("a")
    patch_run["a"] = _result("a", rec="treatment_wins", p_value=0.001, mean_score=0.9)
    patch_run["holdout:a"] = _result("a", rec="treatment_wins", p_value=0.01, mean_score=0.9)

    out = await run_evo(base=base, candidates=cands, golden_set_path=_golden(tmp_path, n=8),
                        config=EvoConfig(eval_limit=4, holdout_limit=4))
    assert out.holdout_disjoint is True
