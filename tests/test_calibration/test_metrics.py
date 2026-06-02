"""Tests for calibration quality metrics (Verified Autonomy L4)."""

from __future__ import annotations

import pytest

from genesis.calibration.metrics import compute_ece, compute_mce


class TestComputeECE:
    def test_empty_returns_zero(self):
        assert compute_ece([]) == 0.0

    def test_zero_samples_returns_zero(self):
        curves = [{"sample_count": 0, "actual_success_rate": 0.5, "predicted_confidence": 0.7}]
        assert compute_ece(curves) == 0.0

    def test_perfectly_calibrated(self):
        """When actual == predicted for all buckets, ECE is 0."""
        curves = [
            {"sample_count": 50, "actual_success_rate": 0.3, "predicted_confidence": 0.3},
            {"sample_count": 50, "actual_success_rate": 0.7, "predicted_confidence": 0.7},
            {"sample_count": 50, "actual_success_rate": 0.9, "predicted_confidence": 0.9},
        ]
        assert compute_ece(curves) == 0.0

    def test_uniform_miscalibration(self):
        """Every bucket off by 0.1 -> ECE = 0.1."""
        curves = [
            {"sample_count": 100, "actual_success_rate": 0.6, "predicted_confidence": 0.7},
            {"sample_count": 100, "actual_success_rate": 0.8, "predicted_confidence": 0.9},
        ]
        assert compute_ece(curves) == pytest.approx(0.1, abs=0.001)

    def test_weighted_by_sample_count(self):
        """Larger buckets contribute more to ECE."""
        curves = [
            {"sample_count": 10, "actual_success_rate": 0.0, "predicted_confidence": 1.0},  # gap=1.0
            {"sample_count": 90, "actual_success_rate": 0.5, "predicted_confidence": 0.5},  # gap=0.0
        ]
        # ECE = (10/100)*1.0 + (90/100)*0.0 = 0.1
        assert compute_ece(curves) == pytest.approx(0.1, abs=0.001)

    def test_single_bucket(self):
        curves = [{"sample_count": 42, "actual_success_rate": 0.65, "predicted_confidence": 0.75}]
        assert compute_ece(curves) == pytest.approx(0.1, abs=0.001)

    def test_severe_miscalibration(self):
        """ResNet-110 CIFAR-100 style: ECE around 12.75%."""
        # Simulated: model reports high confidence but accuracy is moderate
        curves = [
            {"sample_count": 200, "actual_success_rate": 0.72, "predicted_confidence": 0.85},
            {"sample_count": 300, "actual_success_rate": 0.60, "predicted_confidence": 0.75},
            {"sample_count": 500, "actual_success_rate": 0.88, "predicted_confidence": 0.95},
        ]
        ece = compute_ece(curves)
        assert 0.05 < ece < 0.20  # moderate miscalibration range


class TestComputeMCE:
    def test_empty_returns_zero(self):
        assert compute_mce([]) == 0.0

    def test_perfectly_calibrated(self):
        curves = [
            {"sample_count": 50, "actual_success_rate": 0.5, "predicted_confidence": 0.5},
        ]
        assert compute_mce(curves) == 0.0

    def test_worst_bucket_dominates(self):
        curves = [
            {"sample_count": 100, "actual_success_rate": 0.5, "predicted_confidence": 0.5},  # gap=0
            {"sample_count": 10, "actual_success_rate": 0.2, "predicted_confidence": 0.8},   # gap=0.6
        ]
        assert compute_mce(curves) == pytest.approx(0.6, abs=0.001)
