"""Tests for calibration domain types."""

import pytest

from genesis.calibration.types import (
    CalibrationCurve,
    CalibrationDomain,
    PredictionRecord,
    bucket_confidence,
)


def test_domain_values():
    assert CalibrationDomain.OUTREACH == "outreach"
    assert CalibrationDomain.TRIAGE == "triage"
    assert CalibrationDomain.PROCEDURE == "procedure"
    assert CalibrationDomain.ROUTING == "routing"


def test_prediction_record_frozen():
    rec = PredictionRecord(
        id="pred-1",
        action_id="act-1",
        timestamp="2026-03-12T00:00:00Z",
        prediction="surplus outreach will be engaged",
        confidence=0.75,
        confidence_bucket="0.7-0.8",
        domain=CalibrationDomain.OUTREACH,
        reasoning="High-confidence topic for this user",
    )
    assert rec.confidence_bucket == "0.7-0.8"
    assert rec.outcome is None
    assert rec.correct is None


def test_calibration_curve():
    curve = CalibrationCurve(
        domain=CalibrationDomain.OUTREACH,
        confidence_bucket="0.7-0.8",
        predicted_confidence=0.75,
        actual_success_rate=0.60,
        sample_count=52,
        correction_factor=0.80,
    )
    assert curve.correction_factor == pytest.approx(0.80)
    assert curve.sample_count >= 50


def test_bucket_confidence_normal():
    assert bucket_confidence(0.75) == "0.7-0.8"
    assert bucket_confidence(0.85) == "0.8-0.9"
    assert bucket_confidence(0.15) == "0.1-0.2"


def test_bucket_confidence_edge():
    assert bucket_confidence(1.0) == "0.9-1.0"
    assert bucket_confidence(0.0) == "0.0-0.1"
