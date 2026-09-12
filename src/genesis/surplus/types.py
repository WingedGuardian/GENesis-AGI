"""Surplus infrastructure type definitions — enums, frozen dataclasses, protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class TaskType(StrEnum):
    BRAINSTORM_USER = "brainstorm_user"
    BRAINSTORM_SELF = "brainstorm_self"
    META_BRAINSTORM = "meta_brainstorm"
    # GROUNDWORK(v4-surplus-tasks): V4 adds these task types
    MEMORY_AUDIT = "memory_audit"
    PROCEDURE_AUDIT = "procedure_audit"
    GAP_CLUSTERING = "gap_clustering"
    SELF_UNBLOCK = "self_unblock"
    ANTICIPATORY_RESEARCH = "anticipatory_research"
    # Pipeline intermediate step (anticipatory_research pipeline)
    RESEARCH_QUERY_GEN = "research_query_gen"
    PROMPT_EFFECTIVENESS_REVIEW = "prompt_effectiveness_review"
    # Pipeline intermediate steps (prompt_effectiveness pipeline)
    PROMPT_REVIEW_CATALOG = "prompt_review_catalog"
    PROMPT_REVIEW_SAMPLE = "prompt_review_sample"
    CODE_AUDIT = "code_audit"
    INFRASTRUCTURE_MONITOR = "infrastructure_monitor"
    BOOKMARK_ENRICHMENT = "bookmark_enrichment"
    CODE_INDEX = "code_index"
    # Phase 1 surplus expansion: eval + infrastructure maintenance
    MODEL_EVAL = "model_eval"
    DISK_CLEANUP = "disk_cleanup"
    DB_MAINTENANCE = "db_maintenance"
    DEAD_LETTER_REPLAY = "dead_letter_replay"
    BACKUP_VERIFICATION = "backup_verification"
    # J-9 paper eval infrastructure
    J9_EVAL_BATCH = "j9_eval_batch"
    # Memory taxonomy hygiene
    WING_AUDIT = "wing_audit"
    # CC memory file staleness detection
    CC_MEMORY_STALENESS = "cc_memory_staleness"
    # Measurement batch: fresh session diagnostic
    FRESH_SESSION_TEST = "fresh_session_test"


# Task types whose *success* means "produced a useful insight that belongs in the
# knowledge base", as opposed to "an action ran to completion". Only these are
# eligible for the verified-correctness verdict (``outcome_quality``): when one of
# them completes, its FULL output is graded by the measurement-only LLM quality
# judge (surplus.quality_judge). 'useful' = the judge passed the output; 'hollow'
# = the judge scored it below the output_quality threshold (it ran but produced
# nothing of value) — and the Outcome Bus records a VERIFICATION_FAILED negative
# alongside the usual EXECUTION_OUTCOME positive. NULL = a judge outage, a
# non-insight type, or empty/too-short output (positive-only, not penalized).
# (The judge REPLACED an earlier intake-routing heuristic — 'hollow' = intake
# discarded every finding — which was structurally unreachable: curated surplus
# sources skip scoring and route at a fixed 0.6 confidence, so intake never
# discarded everything, so 'hollow' could never fire.)
#
# Deliberately EXCLUDED (would manufacture false negatives / are not KB-bound):
#   - Action tasks (CODE_INDEX, MODEL_EVAL, DISK_CLEANUP, DB_MAINTENANCE,
#     DEAD_LETTER_REPLAY, BACKUP_VERIFICATION, J9_EVAL_BATCH, FRESH_SESSION_TEST):
#     success = the action ran; they don't target the KB, so all-discard is normal.
#   - Pipeline intermediates (RESEARCH_QUERY_GEN, PROMPT_REVIEW_CATALOG,
#     PROMPT_REVIEW_SAMPLE): their output feeds the *next* pipeline step, not the
#     KB, so intake legitimately discards it. Of the pipeline *terminals*, only
#     ANTICIPATORY_RESEARCH is KB-bound; PROMPT_EFFECTIVENESS_REVIEW is first-party
#     ideation, STAGED not KB-routed (see EPHEMERAL_IDEATION_TASK_TYPES below).
#   - Monitoring/probe types (INFRASTRUCTURE_MONITOR, CC_MEMORY_STALENESS): a
#     "nothing noteworthy / all healthy" pass is the EXPECTED good outcome, and
#     their status content isn't durable knowledge — all-discard isn't a failure.
#   - BOOKMARK_ENRICHMENT: uses a dedicated executor whose intake routing is not
#     yet validated (no live volume). Excluded NULL-on-uncertainty (matches the
#     codebase's conservative-classification norm); add once its routing is proven.
INSIGHT_PRODUCING_TASK_TYPES: frozenset[TaskType] = frozenset({
    TaskType.BRAINSTORM_USER,
    TaskType.BRAINSTORM_SELF,
    TaskType.META_BRAINSTORM,
    TaskType.MEMORY_AUDIT,
    TaskType.PROCEDURE_AUDIT,
    TaskType.GAP_CLUSTERING,
    TaskType.SELF_UNBLOCK,
    TaskType.ANTICIPATORY_RESEARCH,
    TaskType.PROMPT_EFFECTIVENESS_REVIEW,
    TaskType.CODE_AUDIT,
    TaskType.WING_AUDIT,
})


# Task types whose completed output should be routed to the KNOWLEDGE BASE via
# the intake pipeline (surplus/dispatch.py::_route_insights). This is a DISTINCT
# concern from INSIGHT_PRODUCING_TASK_TYPES above (the measurement-JUDGE set):
# KB-routing asks "does this task produce durable knowledge worth ingesting?",
# whereas the judge set asks "can this task's output be graded for verified
# correctness?". They mostly overlap, but BOOKMARK_ENRICHMENT is deliberately
# EXCLUDED from the judge set (no live volume to validate its routing — see the
# note above) while it DOES produce KB-bound content (enriched user bookmarks), so
# it belongs here. Keeping the two sets separate avoids silently dropping
# bookmark-enrichment KB writes when the judge set changes.
#
# Everything NOT in this set — action/maintenance tasks (MODEL_EVAL, DISK_CLEANUP,
# DB_MAINTENANCE, DEAD_LETTER_REPLAY, BACKUP_VERIFICATION, J9_EVAL_BATCH,
# FRESH_SESSION_TEST), monitors (INFRASTRUCTURE_MONITOR, CC_MEMORY_STALENESS), and
# pipeline intermediates (RESEARCH_QUERY_GEN, PROMPT_REVIEW_*) — produces
# point-in-time OPERATIONAL TELEMETRY, not durable knowledge: its output must NOT
# be ingested into the knowledge base (it polluted the KB to 71% surplus before
# this gate; the types' own docstring already says they "don't target the KB").
KB_ROUTING_TASK_TYPES: frozenset[TaskType] = INSIGHT_PRODUCING_TASK_TYPES | {
    TaskType.BOOKMARK_ENRICHMENT,
}


# EPHEMERAL first-party ideation — self-directed brainstorm / unblock / audit
# output. These are Genesis-authored work-items and system flags, NOT durable
# external knowledge, so the intake pipeline keeps them OUT of the immortal
# knowledge_units KB (PR-1's intent). They stay in KB_ROUTING_TASK_TYPES (so
# dispatch still calls run_intake); run_intake's Step 3a reroutes them off the
# KB path by checking is_ephemeral_ideation() on the TRUE task type. EXCLUDES
# ANTICIPATORY_RESEARCH (genuine web-cited knowledge) and CODE_AUDIT (its own
# curated FindingsBridge ingestion path — see dispatch.py::_route_insights).
#
# WS-M PR-2 splits ephemeral ideation into TWO populations with distinct honest
# homes (content-verified: the two are genuinely different animals):
#   • IDEA_TASK_TYPES — genuine feature ideas (brainstorm output) → staged in
#     surplus_insights, then promoted into the follow_ups 'idea' review lane.
#   • SELF_OBSERVATION_TASK_TYPES — meta-patterns / audits / blocker analyses
#     about Genesis's own state → the observation lane (TTL + resolve +
#     dashboard/morning-report surfacing), via _route_to_observation.
# A type MUST be in exactly ONE subset; EPHEMERAL_IDEATION_TASK_TYPES is their
# union (kept as the Step-3a intercept guard so neither subset falls through to
# the KB route loop).
IDEA_TASK_TYPES: frozenset[TaskType] = frozenset({
    TaskType.BRAINSTORM_SELF,
    TaskType.BRAINSTORM_USER,
    TaskType.META_BRAINSTORM,
})

SELF_OBSERVATION_TASK_TYPES: frozenset[TaskType] = frozenset({
    TaskType.SELF_UNBLOCK,
    TaskType.GAP_CLUSTERING,
    TaskType.WING_AUDIT,
    TaskType.MEMORY_AUDIT,
    TaskType.PROCEDURE_AUDIT,
    TaskType.PROMPT_EFFECTIVENESS_REVIEW,
})

EPHEMERAL_IDEATION_TASK_TYPES: frozenset[TaskType] = (
    IDEA_TASK_TYPES | SELF_OBSERVATION_TASK_TYPES
)


def is_ephemeral_ideation(task_type: str) -> bool:
    """True if a surplus task-type string is ephemeral first-party ideation.

    Accepts the raw task-type string (e.g. ``"self_unblock"``) as carried on
    ``ScoredFinding.source_task_type``; unknown/empty strings → False (default
    to durable routing — never over-shelve on an unrecognised type).
    """
    try:
        return TaskType(task_type) in EPHEMERAL_IDEATION_TASK_TYPES
    except ValueError:
        return False


def is_idea_ideation(task_type: str) -> bool:
    """True if the surplus task-type is a genuine feature IDEA (brainstorm).

    Ideas are staged in surplus_insights and promoted into the follow_ups
    'idea' review lane. Unknown/empty strings → False.
    """
    try:
        return TaskType(task_type) in IDEA_TASK_TYPES
    except ValueError:
        return False


def is_self_observation_ideation(task_type: str) -> bool:
    """True if the surplus task-type is a self-observation (audit / gap-cluster /
    unblock / prompt-review) routed to the observation lane. Unknown/empty → False.
    """
    try:
        return TaskType(task_type) in SELF_OBSERVATION_TASK_TYPES
    except ValueError:
        return False


class ComputeTier(StrEnum):
    LOCAL_30B = "local_30b"
    FREE_API = "free_api"
    CHEAP_PAID = "cheap_paid"
    NEVER = "never"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class SurplusTask:
    id: str
    task_type: TaskType
    compute_tier: ComputeTier
    priority: float
    drive_alignment: str
    status: TaskStatus
    created_at: str
    payload: str | None = None
    attempt_count: int = 0


@dataclass(frozen=True)
class ExecutorResult:
    success: bool
    content: str | None = None
    insights: list[dict] = field(default_factory=list)
    error: str | None = None


class SurplusExecutor(Protocol):
    async def execute(self, task: SurplusTask) -> ExecutorResult: ...
