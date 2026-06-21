"""Tests for the paired-comparison stats (McNemar exact, pure stdlib)."""

import pytest

from genesis.eval.stats import (
    _mcnemar_exact_two_sided,
    compute_score_winrate,
    compute_winrate,
)


def test_mcnemar_exact_known_values():
    # All 6 discordant pairs favour one arm: 2 * C(6,0)/2^6 = 2/64.
    assert _mcnemar_exact_two_sided(6, 0) == pytest.approx(0.03125)
    # 5 discordant, all one way: 2 * C(5,0)/2^5 = 2/32.
    assert _mcnemar_exact_two_sided(5, 0) == pytest.approx(0.0625)
    # Even split caps at 1.0.
    assert _mcnemar_exact_two_sided(4, 2) == pytest.approx(1.0)


# ---- compute_winrate (binary pass/fail) ----

def test_winrate_length_mismatch_raises():
    with pytest.raises(ValueError, match="same length"):
        compute_winrate([True, False], [True])


def test_winrate_empty_raises():
    with pytest.raises(ValueError, match="no cases"):
        compute_winrate([], [])


def test_winrate_no_discordant_is_insufficient():
    out = compute_winrate([True, True, False], [True, True, False])
    assert out["n_discordant"] == 0
    assert out["p_value"] is None
    assert out["recommendation"] == "insufficient_data"


def test_winrate_six_discordant_treatment_wins_significant():
    out = compute_winrate([False] * 6, [True] * 6)
    assert out["n_treatment_wins"] == 6
    assert out["n_control_wins"] == 0
    assert out["p_value"] == pytest.approx(0.0312)  # rounded to 4dp from 0.03125
    assert out["significant"] is True
    assert out["recommendation"] == "treatment_wins"


def test_winrate_five_discordant_not_significant():
    # Boundary: 5 discordant all one way -> p=0.0625 -> no_difference (not insufficient).
    out = compute_winrate([False] * 5, [True] * 5)
    assert out["n_discordant"] == 5
    assert out["significant"] is False
    assert out["recommendation"] == "no_difference"


def test_winrate_four_discordant_insufficient():
    out = compute_winrate([False] * 4, [True] * 4)
    assert out["recommendation"] == "insufficient_data"


# ---- compute_score_winrate (continuous judge scores) ----

def test_score_winrate_control_wins_significant():
    # The case[0] pattern: control consistently higher, both sub-threshold.
    out = compute_score_winrate([0.15] * 6, [0.05] * 6)
    assert out["n_control_wins"] == 6
    assert out["n_treatment_wins"] == 0
    assert out["control_mean_score"] == pytest.approx(0.15)
    assert out["treatment_mean_score"] == pytest.approx(0.05)
    assert out["p_value"] == pytest.approx(0.0312)  # rounded to 4dp from 0.03125
    assert out["significant"] is True
    assert out["recommendation"] == "control_wins"


def test_score_winrate_equal_scores_are_ties():
    out = compute_score_winrate([0.5] * 6, [0.5] * 6)
    assert out["n_ties"] == 6
    assert out["n_discordant"] == 0
    assert out["recommendation"] == "insufficient_data"


def test_score_winrate_epsilon_creates_ties():
    # 0.02 difference is a win at epsilon=0 but a tie at epsilon=0.05.
    strict = compute_score_winrate([0.52] * 6, [0.50] * 6, epsilon=0.0)
    assert strict["n_control_wins"] == 6
    loose = compute_score_winrate([0.52] * 6, [0.50] * 6, epsilon=0.05)
    assert loose["n_ties"] == 6


def test_score_winrate_length_mismatch_raises():
    with pytest.raises(ValueError, match="same length"):
        compute_score_winrate([0.1, 0.2], [0.1])
