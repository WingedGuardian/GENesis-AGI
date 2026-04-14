"""Eval harness type definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ScorerType(StrEnum):
    EXACT_MATCH = "exact_match"
    JSON_FIELD_MATCH = "json_field_match"
    SET_OVERLAP = "set_overlap"
    JSON_VALIDITY = "json_validity"
    SLOP_DETECTION = "slop_detection"


class EvalTrigger(StrEnum):
    MANUAL = "manual"
    SURPLUS = "surplus"
    SCHEDULE = "schedule"


class TaskCategory(StrEnum):
    CLASSIFICATION = "classification"
    EXTRACTION = "extraction"
    STRUCTURED_OUTPUT = "structured_output"
    SUMMARIZATION = "summarization"
    REASONING = "reasoning"


@dataclass(frozen=True)
class EvalCase:
    """A single test case in a golden dataset."""

    id: str
    input_text: str
    expected_output: str
    scorer_type: ScorerType
    scorer_config: dict = field(default_factory=dict)
    category: TaskCategory = TaskCategory.CLASSIFICATION
    description: str = ""


@dataclass(frozen=True)
class ScoredOutput:
    """Result of scoring a single model output against expected."""

    case_id: str
    passed: bool
    score: float  # 1.0 = pass, 0.0 = fail
    actual_output: str
    scorer_type: ScorerType
    scorer_detail: str = ""
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    skipped: bool = False  # True = API call failed (rate limit/error), not a scoring result


@dataclass(frozen=True)
class EvalRunSummary:
    """Aggregate results of an eval run."""

    run_id: str
    model_id: str
    model_profile: str
    dataset: str
    trigger: EvalTrigger
    task_category: TaskCategory
    total_cases: int
    passed_cases: int
    failed_cases: int
    skipped_cases: int = 0
    aggregate_score: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    duration_s: float = 0.0
    results: list[ScoredOutput] = field(default_factory=list)
