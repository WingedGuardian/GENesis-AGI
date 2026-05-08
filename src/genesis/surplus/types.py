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
