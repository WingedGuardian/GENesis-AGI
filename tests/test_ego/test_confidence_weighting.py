"""Tests for inverse confidence weighting (Verified Autonomy Layer 1)."""

from __future__ import annotations

import pytest

from genesis.ego.confidence_weighting import inverse_confidence_weight


class TestInverseConfidenceWeight:
    def test_empty_dict_returns_zero(self):
        assert inverse_confidence_weight({}) == 0.0

    def test_single_field_identity(self):
        assert inverse_confidence_weight({"a": 0.7}) == pytest.approx(0.7)

    def test_equal_confidence_identity(self):
        """All fields at same value -> that value (no distortion)."""
        result = inverse_confidence_weight({"a": 0.5, "b": 0.5, "c": 0.5})
        assert result == pytest.approx(0.5)

    def test_equal_high_confidence(self):
        result = inverse_confidence_weight({"a": 0.9, "b": 0.9})
        assert result == pytest.approx(0.9)

    def test_weak_field_pulls_aggregate_down(self):
        """One weak field among strong ones pulls the aggregate below mean."""
        scores = {"a": 0.95, "b": 0.92, "c": 0.88, "d": 0.30}
        result = inverse_confidence_weight(scores)
        arithmetic_mean = sum(scores.values()) / len(scores)
        assert result < arithmetic_mean

    def test_boundary_zero_and_one(self):
        """Paper's stated boundary case: {0.0, 1.0} -> 0.333."""
        result = inverse_confidence_weight({"a": 0.0, "b": 1.0})
        assert result == pytest.approx(1 / 3, abs=0.001)

    def test_result_bounded_between_min_and_max(self):
        scores = {"a": 0.2, "b": 0.8, "c": 0.5}
        result = inverse_confidence_weight(scores)
        assert min(scores.values()) <= result <= max(scores.values())

    def test_result_always_leq_arithmetic_mean(self):
        """Inverse-weighted aggregate is always <= arithmetic mean for non-uniform values."""
        scores = {"a": 0.95, "b": 0.30}
        result = inverse_confidence_weight(scores)
        mean = sum(scores.values()) / len(scores)
        assert result <= mean + 1e-9  # tolerance for float

    def test_near_zero_field_dominates(self):
        """Near-zero field among strong fields drops the aggregate sharply."""
        scores = {"a": 0.92, "b": 0.95, "c": 0.01}
        result = inverse_confidence_weight(scores)
        assert result < 0.5  # well below the 0.62 arithmetic mean

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError, match="outside"):
            inverse_confidence_weight({"a": 1.5})

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="outside"):
            inverse_confidence_weight({"a": -0.1})

    def test_all_zero(self):
        result = inverse_confidence_weight({"a": 0.0, "b": 0.0})
        assert result == pytest.approx(0.0)

    def test_all_one(self):
        result = inverse_confidence_weight({"a": 1.0, "b": 1.0})
        assert result == pytest.approx(1.0)

    def test_many_fields(self):
        """Works with many fields."""
        scores = {f"f{i}": 0.8 for i in range(20)}
        scores["weak"] = 0.1
        result = inverse_confidence_weight(scores)
        assert result < 0.8  # weak field pulls down
