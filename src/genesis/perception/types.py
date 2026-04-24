"""Perception type definitions — output contracts, pipeline types, protocols."""

from __future__ import annotations

from dataclasses import dataclass, field  # noqa: F401 — field used by downstream modules

# Shared constants for light reflection focus rotation and quality gates.
# Used by both perception engine (context.py, writer.py) and CC bridge
# (reflection_bridge.py). Defined once to prevent drift.
LIGHT_FOCUS_ROTATION: list[str] = ["situation", "user_impact", "anomaly"]
MIN_DELTA_CONFIDENCE: float = 0.90


@dataclass(frozen=True)
class MicroOutput:
    """Output contract for Micro reflection."""

    tags: list[str]
    salience: float
    anomaly: bool
    summary: str
    signals_examined: int


@dataclass(frozen=True)
class UserModelDelta:
    """A proposed update to the user model cache."""

    field: str
    value: str
    evidence: str
    confidence: float


@dataclass(frozen=True)
class LightOutput:
    """Output contract for Light reflection."""

    assessment: str
    patterns: list[str]
    user_model_updates: list[UserModelDelta]
    recommendations: list[str]
    confidence: float
    focus_area: str
    escalate_to_deep: bool = False
    escalation_reason: str | None = None
    surplus_candidates: list[str] = field(default_factory=list)
    context_update: str | None = None


@dataclass(frozen=True)
class ReflectionResult:
    """Result of a reflection attempt."""

    success: bool
    output: MicroOutput | LightOutput | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PromptContext:
    """Assembled context for prompt rendering."""

    depth: str
    identity: str
    signals_text: str
    tick_number: int
    user_profile: str | None = None
    cognitive_state: str | None = None
    memory_hits: str | None = None
    prior_context: str | None = None
    user_model: str | None = None
    suggested_focus: str | None = None
    calibration_text: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    """Response from an LLM call with metadata."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
