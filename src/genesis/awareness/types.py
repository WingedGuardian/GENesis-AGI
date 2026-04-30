"""Data types for the Awareness Loop."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Depth(StrEnum):
    """Reflection depth levels. Values match DB seed data in signal_weights.feeds_depths."""

    MICRO = "Micro"
    LIGHT = "Light"
    DEEP = "Deep"
    STRATEGIC = "Strategic"


@dataclass(frozen=True)
class SignalReading:
    """A single signal measurement."""

    name: str
    value: float  # 0.0–1.0 normalized
    source: str
    collected_at: str  # ISO datetime
    failed: bool = False  # True when collector returned a stub reading on error
    # Optional threshold context for LLM consumers (signal calibration)
    normal_max: float | None = None  # values at or below this are normal
    warning_threshold: float | None = None  # values at or above this warrant attention
    critical_threshold: float | None = None  # values at or above this are critical
    baseline_note: str | None = None  # human-readable "what's normal" for LLM context


@dataclass(frozen=True)
class DepthScore:
    """Urgency score for one depth level."""

    depth: Depth
    raw_score: float
    time_multiplier: float
    final_score: float  # raw_score × time_multiplier
    threshold: float
    triggered: bool  # final_score >= threshold


@dataclass(frozen=True)
class TickResult:
    """Complete result of one awareness tick."""

    tick_id: str
    timestamp: str  # ISO datetime
    source: str  # "scheduled" | "critical_bypass"
    signals: list[SignalReading]
    scores: list[DepthScore]
    classified_depth: Depth | None
    trigger_reason: str | None
    escalation_source: str | None = None
    escalation_pending_id: str | None = None  # observation ID to resolve after dispatch
    signal_staleness: dict[str, int] | None = None  # signal_name → consecutive unchanged ticks
