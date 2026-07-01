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
#     KB, so intake legitimately discards it. Only the pipeline *terminals*
#     (ANTICIPATORY_RESEARCH, PROMPT_EFFECTIVENESS_REVIEW) produce KB-bound insight.
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
