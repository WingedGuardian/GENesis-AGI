"""Calibration quality metrics — pure functions, no dependencies.

Computes Expected Calibration Error (ECE) and Maximum Calibration Error
(MCE) from calibration curve data. These are monitoring metrics that
quantify how well confidence scores track actual accuracy.

Reference: Guo et al. (2017), "On Calibration of Modern Neural Networks"
Verified Autonomy Layer 4: doi.org/10.5281/zenodo.19096229, Section 7
"""

from __future__ import annotations


def compute_ece(curves: list[dict]) -> float:
    """Expected Calibration Error from calibration curve data.

    ECE = Σ (bucket_weight × |actual_success_rate - predicted_confidence|)

    Parameters
    ----------
    curves:
        List of dicts from ``CalibrationCurveComputer.compute()``, each with
        ``sample_count``, ``actual_success_rate``, ``predicted_confidence``.

    Returns
    -------
    float:
        ECE in [0.0, 1.0]. Lower is better. 0.0 means perfectly calibrated.
        Returns 0.0 for empty input.
    """
    if not curves:
        return 0.0
    total = sum(c["sample_count"] for c in curves)
    if total == 0:
        return 0.0
    return round(
        sum(
            (c["sample_count"] / total)
            * abs(c["actual_success_rate"] - c["predicted_confidence"])
            for c in curves
        ),
        4,
    )


def compute_mce(curves: list[dict]) -> float:
    """Maximum Calibration Error — worst-case bucket miscalibration.

    MCE = max over buckets of |actual_success_rate - predicted_confidence|

    Returns 0.0 for empty input.
    """
    if not curves:
        return 0.0
    return round(
        max(
            abs(c["actual_success_rate"] - c["predicted_confidence"])
            for c in curves
        ),
        4,
    )
