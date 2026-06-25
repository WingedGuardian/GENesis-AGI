"""Paired-comparison statistics for cognitive A/B experiments.

Pure-stdlib (no scipy/numpy) significance testing for the experimentation
harness. The control and treatment arms run on the *same* golden cases with
binary (pass/fail) outcomes, so the right test is McNemar's on the discordant
pairs. For the small N of a golden set (tens of cases) we use the *exact*
binomial form, not the chi-squared approximation.

This lives in ``eval/`` (general eval infrastructure, beside ``calibration.py``)
rather than ``experimentation/`` so any paired-eval caller can use it.
"""

from __future__ import annotations

import math

# Below this many discordant pairs the test has no power — report it honestly
# rather than emitting a misleading "no_difference".
_MIN_DISCORDANT = 5
_ALPHA = 0.05


def compute_winrate(
    control_results: list[bool],
    treatment_results: list[bool],
) -> dict:
    """Paired win-rate statistics for an A/B over the same golden cases.

    Parameters
    ----------
    control_results, treatment_results:
        Per-case pass (True) / fail (False) for each arm, in the SAME case
        order. Must be equal length.

    Returns
    -------
    dict with:
        n_cases, control_pass_rate, treatment_pass_rate, treatment_delta,
        n_concordant_pass, n_concordant_fail,
        n_control_wins, n_treatment_wins, n_discordant,
        p_value (McNemar exact two-sided; None if no discordant pairs),
        significant (bool), effect_size,
        recommendation: "treatment_wins" | "control_wins" | "no_difference"
                        | "insufficient_data"
    """
    if len(control_results) != len(treatment_results):
        msg = (
            f"control ({len(control_results)}) and treatment "
            f"({len(treatment_results)}) must be the same length"
        )
        raise ValueError(msg)
    n = len(control_results)
    if n == 0:
        raise ValueError("no cases to compare")

    pairs = list(zip(control_results, treatment_results, strict=True))
    both_pass = sum(1 for c, t in pairs if c and t)
    both_fail = sum(1 for c, t in pairs if not c and not t)
    control_wins = sum(1 for c, t in pairs if c and not t)  # control pass, treatment fail
    treatment_wins = sum(1 for c, t in pairs if not c and t)  # control fail, treatment pass
    n_discordant = control_wins + treatment_wins

    control_pass_rate = sum(1 for c, _ in pairs if c) / n
    treatment_pass_rate = sum(1 for _, t in pairs if t) / n

    p_value: float | None = None
    if n_discordant > 0:
        p_value = _mcnemar_exact_two_sided(n_discordant, min(control_wins, treatment_wins))

    significant = p_value is not None and p_value < _ALPHA
    # Asymmetry of the discordant pairs: 0.0 = even split, 1.0 = all one way.
    effect_size = abs(treatment_wins - control_wins) / n_discordant if n_discordant else 0.0

    if n_discordant < _MIN_DISCORDANT:
        recommendation = "insufficient_data"
    elif not significant:
        recommendation = "no_difference"
    elif treatment_wins > control_wins:
        recommendation = "treatment_wins"
    else:
        recommendation = "control_wins"

    return {
        "n_cases": n,
        "control_pass_rate": round(control_pass_rate, 4),
        "treatment_pass_rate": round(treatment_pass_rate, 4),
        "treatment_delta": round(treatment_pass_rate - control_pass_rate, 4),
        "n_concordant_pass": both_pass,
        "n_concordant_fail": both_fail,
        "n_control_wins": control_wins,
        "n_treatment_wins": treatment_wins,
        "n_discordant": n_discordant,
        "p_value": round(p_value, 4) if p_value is not None else None,
        "significant": significant,
        "effect_size": round(effect_size, 4),
        "recommendation": recommendation,
    }


def compute_score_winrate(
    control_scores: list[float],
    treatment_scores: list[float],
    *,
    epsilon: float = 0.0,
) -> dict:
    """Paired win-rate over CONTINUOUS per-case judge scores.

    More sensitive than `compute_winrate` for graded outputs: a case is a
    "win" for whichever arm scored higher (by more than ``epsilon``), a tie
    otherwise. This captures quality differences that a fixed pass/fail
    threshold collapses (e.g. control 0.15 vs treatment 0.05 — both "fail" a
    0.6 gate, yet control is clearly better). McNemar exact on the wins.

    Returns the same shape as `compute_winrate`, with mean-score fields in
    place of pass-rate fields and an ``n_ties`` count.
    """
    if len(control_scores) != len(treatment_scores):
        msg = (
            f"control ({len(control_scores)}) and treatment "
            f"({len(treatment_scores)}) must be the same length"
        )
        raise ValueError(msg)
    n = len(control_scores)
    if n == 0:
        raise ValueError("no cases to compare")

    pairs = list(zip(control_scores, treatment_scores, strict=True))
    control_wins = sum(1 for c, t in pairs if c - t > epsilon)
    treatment_wins = sum(1 for c, t in pairs if t - c > epsilon)
    ties = n - control_wins - treatment_wins
    n_discordant = control_wins + treatment_wins

    control_mean = sum(control_scores) / n
    treatment_mean = sum(treatment_scores) / n

    p_value: float | None = None
    if n_discordant > 0:
        p_value = _mcnemar_exact_two_sided(n_discordant, min(control_wins, treatment_wins))

    significant = p_value is not None and p_value < _ALPHA
    effect_size = abs(treatment_wins - control_wins) / n_discordant if n_discordant else 0.0

    if n_discordant < _MIN_DISCORDANT:
        recommendation = "insufficient_data"
    elif not significant:
        recommendation = "no_difference"
    elif treatment_wins > control_wins:
        recommendation = "treatment_wins"
    else:
        recommendation = "control_wins"

    return {
        "n_cases": n,
        "control_mean_score": round(control_mean, 4),
        "treatment_mean_score": round(treatment_mean, 4),
        "mean_delta": round(treatment_mean - control_mean, 4),
        "n_control_wins": control_wins,
        "n_treatment_wins": treatment_wins,
        "n_ties": ties,
        "n_discordant": n_discordant,
        "p_value": round(p_value, 4) if p_value is not None else None,
        "significant": significant,
        "effect_size": round(effect_size, 4),
        "recommendation": recommendation,
    }


def _mcnemar_exact_two_sided(n_discordant: int, k_smaller: int) -> float:
    """Exact two-sided McNemar p-value.

    Under H0 each of ``n_discordant`` discordant pairs independently favours
    either arm with probability 0.5, so the count favouring one arm is
    Binomial(n_discordant, 0.5). The exact two-sided p-value is twice the
    lower tail up to the smaller discordant count (the distribution is
    symmetric), capped at 1.0.

    ``k_smaller = min(control_wins, treatment_wins)``.
    """
    n = n_discordant
    lower_tail = sum(math.comb(n, i) for i in range(k_smaller + 1)) / (2**n)
    return min(1.0, 2.0 * lower_tail)
