"""Inverse confidence weighting — surface your weakest signal.

Implements Layer 1 of the Verified Autonomy framework (Newman et al., 2026).
When aggregating per-field confidence scores, standard averaging masks
uncertainty in individual fields. Inverse weighting gives disproportionate
influence to weak signals so the aggregate reflects the weakest point,
not the average.

Formula: weight = 2.0 - confidence
  - A field at 0.0 confidence gets weight 2.0 (maximum pull)
  - A field at 1.0 confidence gets weight 1.0 (minimum pull)
  - Result is always <= arithmetic mean when values are non-uniform
  - Result always falls between min and max input values

Reference: doi.org/10.5281/zenodo.19096229, Section 4
"""

from __future__ import annotations


def inverse_confidence_weight(scores: dict[str, float]) -> float:
    """Aggregate per-field confidence scores with inverse weighting.

    Parameters
    ----------
    scores:
        Mapping of field names to confidence values in [0.0, 1.0].

    Returns
    -------
    float:
        Weighted aggregate in [0.0, 1.0], pulled toward the weakest signal.
        Returns 0.0 for an empty dict.

    Raises
    ------
    ValueError:
        If any confidence value is outside [0.0, 1.0].
    """
    if not scores:
        return 0.0

    for name, val in scores.items():
        if not (0.0 <= val <= 1.0):
            msg = f"confidence for {name!r} outside [0.0, 1.0]: {val}"
            raise ValueError(msg)

    weights = {k: 2.0 - v for k, v in scores.items()}
    total_weight = sum(weights.values())
    weighted_sum = sum(scores[k] * weights[k] for k in scores)
    return weighted_sum / total_weight
