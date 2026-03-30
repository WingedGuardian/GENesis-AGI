"""Phase 6 Learning Fundamentals — all enums and frozen dataclasses.

Zero external dependencies beyond the stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum

# ─── Enums ────────────────────────────────────────────────────────────────────


class OutcomeClass(StrEnum):
    SUCCESS = "success"
    APPROACH_FAILURE = "approach_failure"
    CAPABILITY_GAP = "capability_gap"
    EXTERNAL_BLOCKER = "external_blocker"
    WORKAROUND_SUCCESS = "workaround_success"
    UNKNOWN = "unknown"


class TriageDepth(IntEnum):
    SKIP = 0
    QUICK_NOTE = 1
    WORTH_THINKING = 2
    FULL_ANALYSIS = 3
    FULL_PLUS_WORKAROUND = 4


class DeltaClassification(StrEnum):
    EXACT_MATCH = "exact_match"
    ACCEPTABLE_SHORTFALL = "acceptable_shortfall"
    OVER_DELIVERY = "over_delivery"
    MISINTERPRETATION = "misinterpretation"


class DiscoveryAttribution(StrEnum):
    EXTERNAL_LIMITATION = "external_limitation"
    USER_MODEL_GAP = "user_model_gap"
    GENESIS_CAPABILITY = "genesis_capability"
    GENESIS_INTERPRETATION = "genesis_interpretation"
    SCOPE_UNDERSPECIFIED = "scope_underspecified"
    USER_REVISED_SCOPE = "user_revised_scope"


class SignalWeightTier(StrEnum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"


class EngagementOutcome(StrEnum):
    ENGAGED = "engaged"
    IGNORED = "ignored"
    NEUTRAL = "neutral"


class MaturityStage(StrEnum):
    EARLY = "early"
    GROWING = "growing"
    MATURE = "mature"


# ─── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InteractionSummary:
    session_id: str
    user_text: str
    response_text: str
    tool_calls: list[str]
    token_count: int
    channel: str
    timestamp: datetime


@dataclass(frozen=True)
class TriageResult:
    depth: TriageDepth
    rationale: str
    skipped_by_prefilter: bool


@dataclass(frozen=True)
class ScopeEvolution:
    original_request: str
    final_delivery: str
    scope_communicated: bool


@dataclass(frozen=True)
class RequestDeliveryDelta:
    classification: DeltaClassification
    attributions: list[DiscoveryAttribution]
    scope_evolution: ScopeEvolution | None
    evidence: str


@dataclass(frozen=True)
class RetrospectiveResult:
    summary: InteractionSummary
    triage: TriageResult
    outcome: OutcomeClass | None
    delta: RequestDeliveryDelta | None
    observations_written: int
    procedures_updated: int


@dataclass(frozen=True)
class ProcedureMatch:
    procedure_id: str
    task_type: str
    confidence: float
    success_count: int
    failure_count: int
    failure_modes: list[dict]
    workarounds: list[dict]
    activation_tier: str = "L4"
    tool_trigger: list[str] | None = None
    steps: list[str] | None = None
    principle: str | None = None


@dataclass(frozen=True)
class CalibrationRules:
    examples: list[dict]
    rules: list[str]
    generated_at: datetime
    source_model: str


@dataclass(frozen=True)
class EngagementSignal:
    channel: str
    outcome: EngagementOutcome
    latency_seconds: float | None
    evidence: str


@dataclass(frozen=True)
class FallbackChain:
    obstacle_type: str
    methods: list[str]
    current_index: int
