"""Tests for the skill-replay verdict — pure statistics, no CC, no clock.

control = OLD, treatment = NEW. The gate promotes only on zero-regression +
strict-improvement; regression is any per-task loss beyond epsilon (or a
pass-rate that favours OLD).
"""

from __future__ import annotations

import pytest

from genesis.eval.skill_replay.types import (
    VERDICT_INCONCLUSIVE,
    VERDICT_NET_POSITIVE,
    VERDICT_REGRESSION,
    SkillReplayConfig,
)
from genesis.eval.skill_replay.verdict import compute_verdict

_CFG = SkillReplayConfig(epsilon=0.05, min_pairs=5)


def test_net_positive_zero_regression_with_improvements():
    # 5 pairs: NEW clearly better on 3, ties on 2 → zero regressions, improved.
    v = compute_verdict(
        old_scores=[0.5, 0.5, 0.5, 0.5, 0.5],
        new_scores=[0.9, 0.9, 0.9, 0.5, 0.5],
        old_pass=[False, False, False, False, False],
        new_pass=[True, True, True, False, False],
        config=_CFG,
    )
    assert v.verdict == VERDICT_NET_POSITIVE
    assert v.n_regressions == 0
    assert v.n_improvements == 3
    assert v.n_complete == 5


def test_regression_on_single_task_score_loss():
    # OLD beats NEW on task 0 (0.9 vs 0.5) → regression even amid ties.
    v = compute_verdict(
        old_scores=[0.9, 0.5, 0.5, 0.5, 0.5],
        new_scores=[0.5, 0.5, 0.5, 0.5, 0.5],
        old_pass=[True, False, False, False, False],
        new_pass=[False, False, False, False, False],
        config=_CFG,
    )
    assert v.verdict == VERDICT_REGRESSION
    assert v.n_regressions == 1


def test_regression_when_pass_rate_favours_old_despite_score_ties():
    # Scores all tie (no per-task score win either way), but OLD passes 6 tasks
    # NEW fails → significant McNemar control_wins → regression via pass-rate.
    old_pass = [True, True, True, True, True, True, False]
    new_pass = [False, False, False, False, False, False, False]
    scores = [0.5] * 7
    v = compute_verdict(
        old_scores=scores,
        new_scores=list(scores),
        old_pass=old_pass,
        new_pass=new_pass,
        config=_CFG,
    )
    assert v.verdict == VERDICT_REGRESSION
    assert v.n_regressions == 0  # no per-task SCORE regression...
    assert v.pass_winrate["recommendation"] == "control_wins"  # ...pass-rate is why


def test_inconclusive_all_ties():
    v = compute_verdict(
        old_scores=[0.5] * 5,
        new_scores=[0.5] * 5,
        old_pass=[True] * 5,
        new_pass=[True] * 5,
        config=_CFG,
    )
    assert v.verdict == VERDICT_INCONCLUSIVE
    assert v.n_regressions == 0
    assert v.n_improvements == 0


def test_inconclusive_below_min_pairs():
    # Clear improvement, but only 2 complete pairs (< min_pairs=5) → no power.
    v = compute_verdict(
        old_scores=[0.5, 0.5],
        new_scores=[0.9, 0.9],
        old_pass=[False, False],
        new_pass=[True, True],
        config=_CFG,
    )
    assert v.verdict == VERDICT_INCONCLUSIVE
    assert v.n_complete == 2
    assert "min_pairs" in v.note


def test_regression_takes_precedence_over_improvement():
    # NEW improves task 0 but regresses task 1 → any regression wins the verdict.
    v = compute_verdict(
        old_scores=[0.5, 0.9, 0.5, 0.5, 0.5],
        new_scores=[0.9, 0.5, 0.5, 0.5, 0.5],
        old_pass=[False, True, False, False, False],
        new_pass=[True, False, False, False, False],
        config=_CFG,
    )
    assert v.verdict == VERDICT_REGRESSION
    assert v.n_regressions == 1
    assert v.n_improvements == 1


def test_epsilon_treats_small_differences_as_ties():
    # NEW is higher but only by 0.03 (< epsilon 0.05) everywhere → all ties.
    v = compute_verdict(
        old_scores=[0.50] * 5,
        new_scores=[0.53] * 5,
        old_pass=[True] * 5,
        new_pass=[True] * 5,
        config=_CFG,
    )
    assert v.verdict == VERDICT_INCONCLUSIVE
    assert v.n_improvements == 0


def test_length_mismatch_raises():
    with pytest.raises(ValueError, match="equal length"):
        compute_verdict(
            old_scores=[0.5, 0.5],
            new_scores=[0.5],
            old_pass=[True, True],
            new_pass=[True],
            config=_CFG,
        )
