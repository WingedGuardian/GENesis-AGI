"""Calibration domain types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CalibrationDomain(StrEnum):
    OUTREACH = "outreach"
    TRIAGE = "triage"
    PROCEDURE = "procedure"
    ROUTING = "routing"


@dataclass(frozen=True)
class PredictionRecord:
    id: str
    action_id: str
    timestamp: str
    prediction: str
    confidence: float
    confidence_bucket: str
    domain: CalibrationDomain
    reasoning: str
    outcome: str | None = None
    correct: bool | None = None
    matched_at: str | None = None


@dataclass(frozen=True)
class CalibrationCurve:
    domain: CalibrationDomain
    confidence_bucket: str
    predicted_confidence: float
    actual_success_rate: float
    sample_count: int
    correction_factor: float


def bucket_confidence(confidence: float) -> str:
    """Bin a 0-1 confidence value into a calibration bucket."""
    clamped = max(0.0, min(confidence, 0.999))
    bucket_start = int(clamped * 10) / 10
    bucket_end = bucket_start + 0.1
    return f"{bucket_start:.1f}-{bucket_end:.1f}"
