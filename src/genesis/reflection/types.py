"""Deep reflection type definitions — enums and frozen dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class DeepReflectionJob(StrEnum):
    """Jobs that a deep reflection cycle can perform."""

    MEMORY_CONSOLIDATION = "memory_consolidation"
    SKILL_REVIEW = "skill_review"
    COST_RECONCILIATION = "cost_reconciliation"
    LESSONS_EXTRACTION = "lessons_extraction"
    COGNITIVE_REGENERATION = "cognitive_regeneration"


class AssessmentDimension(StrEnum):
    """Dimensions evaluated in weekly self-assessment."""

    REFLECTION_QUALITY = "reflection_quality"
    PROCEDURE_EFFECTIVENESS = "procedure_effectiveness"
    OUTREACH_CALIBRATION = "outreach_calibration"
    LEARNING_VELOCITY = "learning_velocity"
    RESOURCE_EFFICIENCY = "resource_efficiency"
    BLIND_SPOTS = "blind_spots"


@dataclass(frozen=True)
class PendingWorkSummary:
    """Summarises which deep reflection jobs have pending work."""

    memory_consolidation: bool = False
    skill_review: bool = False
    cost_reconciliation: bool = False
    lessons_extraction: bool = False
    cognitive_regeneration: bool = False

    # Data counts for prompt assembly (only include sections with data)
    observation_backlog: int = 0
    skills_needing_review: int = 0
    # Intelligence digest for intake pipeline awareness
    intake_items_since_last: int = 0

    @property
    def has_any_work(self) -> bool:
        """True if there's substantive work justifying a deep reflection.

        Cost reconciliation is always-included but doesn't justify triggering
        a reflection on its own.  Cognitive regeneration DOES justify a
        reflection — stale or missing cognitive state is self-healing work.
        """
        return any([
            self.memory_consolidation,
            self.skill_review,
            self.lessons_extraction,
            self.cognitive_regeneration,
        ])

    @property
    def active_jobs(self) -> list[DeepReflectionJob]:
        jobs = []
        if self.memory_consolidation:
            jobs.append(DeepReflectionJob.MEMORY_CONSOLIDATION)
        if self.skill_review:
            jobs.append(DeepReflectionJob.SKILL_REVIEW)
        if self.cost_reconciliation:
            jobs.append(DeepReflectionJob.COST_RECONCILIATION)
        if self.lessons_extraction:
            jobs.append(DeepReflectionJob.LESSONS_EXTRACTION)
        if self.cognitive_regeneration:
            jobs.append(DeepReflectionJob.COGNITIVE_REGENERATION)
        return jobs


@dataclass(frozen=True)
class ProcedureStats:
    """Aggregate statistics for procedural memory."""

    total_active: int = 0
    total_quarantined: int = 0
    avg_success_rate: float = 0.0
    low_performers: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class CostSummary:
    """Cost summary for a time period."""

    daily_usd: float = 0.0
    weekly_usd: float = 0.0
    monthly_usd: float = 0.0
    daily_budget_pct: float = 0.0
    weekly_budget_pct: float = 0.0
    monthly_budget_pct: float = 0.0


@dataclass(frozen=True)
class ContextBundle:
    """All context assembled for a deep reflection invocation."""

    cognitive_state: str = ""
    recent_observations: list[dict] = field(default_factory=list)
    procedure_stats: ProcedureStats = field(default_factory=ProcedureStats)
    intelligence_digest: str = ""
    skill_reports: list[dict] = field(default_factory=list)
    cost_summary: CostSummary = field(default_factory=CostSummary)
    pending_work: PendingWorkSummary = field(default_factory=PendingWorkSummary)
    recent_conversations: list[dict] = field(default_factory=list)
    # IDs of observations retrieved for deep reflection context — used to
    # mark influenced AFTER reflection produces output, not before.
    gathered_observation_ids: tuple[str, ...] = ()


# ── Deep reflection output types ──────────────────────────────────────


@dataclass(frozen=True)
class MemoryOperation:
    """A single memory consolidation operation."""

    operation: str  # "dedup", "merge", "prune", "flag_contradiction"
    target_ids: list[str] = field(default_factory=list)
    reason: str = ""
    merged_content: str | None = None


@dataclass(frozen=True)
class SurplusTaskRequest:
    """A request from deep reflection to enqueue a surplus task."""

    task_type: str  # Valid TaskType value (e.g., "code_audit", "memory_audit")
    reason: str
    priority: float = 0.5
    drive_alignment: str = "competence"
    payload: str | None = None


@dataclass(frozen=True)
class UserQuestion:
    """A question from deep reflection that needs human judgment."""

    text: str
    context: str  # Why this question matters
    options: list[str] = field(default_factory=list)  # 2-4 options when applicable


@dataclass(frozen=True)
class DeepReflectionOutput:
    """Structured output from a deep reflection CC session."""

    observations: list[str] = field(default_factory=list)
    cognitive_state_update: str | None = None
    memory_operations: list[MemoryOperation] = field(default_factory=list)
    skill_triggers: list[str] = field(default_factory=list)
    procedure_quarantines: list[dict] = field(default_factory=list)
    contradictions: list[dict] = field(default_factory=list)
    learnings: list[str] = field(default_factory=list)
    surplus_task_requests: list[SurplusTaskRequest] = field(default_factory=list)
    user_question: UserQuestion | None = None
    focus_next: str = ""
    confidence: float = 0.7
    separability: float | None = None
    alternative_assessment: str | None = None
    parse_failed: bool = False


@dataclass(frozen=True)
class DimensionScore:
    """Score for a single assessment dimension."""

    dimension: AssessmentDimension
    score: float  # 0.0 - 1.0
    evidence: str = ""
    data_available: bool = True


@dataclass(frozen=True)
class WeeklyAssessmentOutput:
    """Structured output from a weekly self-assessment."""

    dimensions: list[DimensionScore] = field(default_factory=list)
    overall_score: float = 0.0
    observations: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    parse_failed: bool = False


@dataclass(frozen=True)
class ProcedureTrend:
    """Trend data for a single procedure."""

    procedure_id: str
    task_type: str
    current_success_rate: float
    previous_success_rate: float
    trend: str  # "improving", "stable", "declining"
    invocations_this_period: int = 0


@dataclass(frozen=True)
class QualityCalibrationOutput:
    """Structured output from weekly quality calibration."""

    drift_detected: bool = False
    per_procedure_trends: list[ProcedureTrend] = field(default_factory=list)
    quarantine_candidates: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    parse_failed: bool = False
