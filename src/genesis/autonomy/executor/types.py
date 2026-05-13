"""Type definitions for the task executor system.

Defines phases, step types, result objects, execution traces,
and the canonical state transition table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Task phases (state machine states)
# ---------------------------------------------------------------------------


class TaskPhase(StrEnum):
    """Lifecycle phases for a background task."""

    PENDING = "pending"
    OBSERVING = "observing"
    REVIEWING = "reviewing"
    PLANNING = "planning"
    EXECUTING = "executing"
    PAUSED = "paused"
    VERIFYING = "verifying"
    SYNTHESIZING = "synthesizing"
    DELIVERING = "delivering"
    RETROSPECTIVE = "retrospective"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Valid state transitions --- engine._transition() validates against this
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: dict[TaskPhase, set[TaskPhase]] = {
    TaskPhase.PENDING: {TaskPhase.OBSERVING, TaskPhase.FAILED, TaskPhase.CANCELLED},
    TaskPhase.OBSERVING: {
        TaskPhase.REVIEWING,
        TaskPhase.BLOCKED,  # stale plan blocks for user review
        TaskPhase.FAILED,
        TaskPhase.CANCELLED,
    },
    TaskPhase.REVIEWING: {
        TaskPhase.PLANNING,
        TaskPhase.BLOCKED,  # plan has gaps, need user input
        TaskPhase.FAILED,
        TaskPhase.CANCELLED,
    },
    TaskPhase.PLANNING: {
        TaskPhase.EXECUTING,
        TaskPhase.FAILED,
        TaskPhase.CANCELLED,
    },
    TaskPhase.EXECUTING: {
        TaskPhase.EXECUTING,  # next step in sequence
        TaskPhase.VERIFYING,
        TaskPhase.PAUSED,
        TaskPhase.BLOCKED,  # mid-step blocker
        TaskPhase.FAILED,
        TaskPhase.CANCELLED,
    },
    TaskPhase.PAUSED: {
        TaskPhase.EXECUTING,  # resume
        TaskPhase.CANCELLED,
    },
    TaskPhase.VERIFYING: {
        TaskPhase.EXECUTING,  # review failed, iterate
        TaskPhase.SYNTHESIZING,  # review passed
        TaskPhase.BLOCKED,  # review cap hit, escalate to user
        TaskPhase.FAILED,
        TaskPhase.CANCELLED,
    },
    TaskPhase.SYNTHESIZING: {
        TaskPhase.DELIVERING,
        TaskPhase.FAILED,
        TaskPhase.CANCELLED,
    },
    TaskPhase.DELIVERING: {
        TaskPhase.RETROSPECTIVE,
        TaskPhase.FAILED,
        TaskPhase.CANCELLED,
    },
    TaskPhase.RETROSPECTIVE: {
        TaskPhase.COMPLETED,
        TaskPhase.FAILED,  # retrospective itself fails (non-blocking)
    },
    TaskPhase.BLOCKED: {
        TaskPhase.OBSERVING,  # resume from observation-phase blocker
        TaskPhase.REVIEWING,  # user responded to review-phase blocker
        TaskPhase.EXECUTING,  # user responded to mid-task blocker
        TaskPhase.VERIFYING,  # recovery resume after verify-phase blocker
        TaskPhase.CANCELLED,
    },
    # Terminal states --- no transitions out
    TaskPhase.COMPLETED: set(),
    TaskPhase.FAILED: set(),
    TaskPhase.CANCELLED: set(),
}


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""


def validate_transition(from_phase: TaskPhase, to_phase: TaskPhase) -> None:
    """Raise :class:`InvalidTransitionError` if the transition is not allowed."""
    allowed = VALID_TRANSITIONS.get(from_phase, set())
    if to_phase not in allowed:
        raise InvalidTransitionError(
            f"Invalid transition: {from_phase.value} -> {to_phase.value}. "
            f"Allowed: {sorted(p.value for p in allowed)}"
        )


# ---------------------------------------------------------------------------
# Step types with default timeouts
# ---------------------------------------------------------------------------


class StepType(StrEnum):
    """Classification of task steps for routing and timeout configuration."""

    RESEARCH = "research"
    CODE = "code"
    ANALYSIS = "analysis"
    SYNTHESIS = "synthesis"
    VERIFICATION = "verification"
    EXTERNAL = "external"

    @property
    def default_timeout_s(self) -> int:
        """Default timeout in seconds for this step type."""
        return _STEP_TIMEOUTS.get(self, 600)

    @property
    def verify_step(self) -> bool:
        """Whether this step type warrants per-step verification.

        # GROUNDWORK(per-step-verify): flag for future per-step
        # verification gates.  CODE and VERIFICATION steps produce
        # verifiable artifacts; others (research, analysis, synthesis,
        # external) are informational and don't need active checking.
        # The follow-up PR will call ``_tool_capable_review()`` after
        # each step where ``verify_step`` is True.
        """
        return self in (StepType.CODE, StepType.VERIFICATION)


# Generous timeouts — executor runs in the background, nobody is waiting.
# A code step may write code + run the full test suite (11+ min observed).
# Prefer letting work finish over killing productive sessions.
_STEP_TIMEOUTS: dict[StepType, int] = {
    StepType.RESEARCH: 3600,      # 60 min — network-dependent, multiple sources
    StepType.CODE: 3600,          # 60 min — write + test + iterate
    StepType.ANALYSIS: 1800,      # 30 min — may read many files
    StepType.SYNTHESIS: 1800,     # 30 min — reading + writing
    StepType.VERIFICATION: 3600,  # 60 min — may run full test suite
    StepType.EXTERNAL: 3600,      # 60 min — unknown external work
}


# ---------------------------------------------------------------------------
# Step result
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    """Result of executing a single task step."""

    idx: int
    status: str  # "completed", "blocked", "failed"
    result: str
    cost_usd: float = 0.0
    session_id: str | None = None
    model_used: str = ""
    duration_s: float = 0.0
    artifacts: list[str] = field(default_factory=list)
    blocker_description: str | None = None


# ---------------------------------------------------------------------------
# Execution trace (matches design doc section 1459-1487)
# ---------------------------------------------------------------------------


@dataclass
class ExecutionTrace:
    """Full trace of a task execution for retrospective learning."""

    task_id: str
    initiated_by: str  # "user" or "genesis"
    user_request: str
    plan: list[str] = field(default_factory=list)
    sub_agents: list[dict] = field(default_factory=list)
    quality_gate: dict = field(default_factory=dict)
    total_cost_usd: float = 0.0
    procedural_extractions: list[str] = field(default_factory=list)
    retrospective_id: str | None = None
    request_delivery_delta: dict = field(default_factory=dict)
    step_results: list[StepResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Protocols for Session 3 implementations (workaround.py, trace.py)
# ---------------------------------------------------------------------------


@runtime_checkable
class WorkaroundSearcher(Protocol):
    """Protocol for the workaround search strategy (implemented in workaround.py)."""

    async def search(
        self, step: dict, error: str, prior_attempts: list[str],
    ) -> WorkaroundResult | None: ...


@dataclass(frozen=True)
class WorkaroundResult:
    """Result of a workaround search attempt."""

    found: bool
    approach: str | None = None


@dataclass(frozen=True)
class ResearchResult:
    """Result of a deep research session investigating a blocker."""

    found: bool
    approach: str | None = None  # actionable approach if found=True
    sources: list[str] = field(default_factory=list)
    clues: str | None = None  # partial findings even when found=False
    concrete_blockers: list[str] = field(default_factory=list)  # what needs to change
    session_id: str | None = None  # research session ID for traceability


@runtime_checkable
class ResearchSearcher(Protocol):
    """Protocol for deep research dispatch (implemented in research.py)."""

    async def inline_due_diligence(
        self, step: dict, error: str,
    ) -> str | None: ...

    async def research(
        self, step: dict, error: str, prior_attempts: list[str],
        due_diligence_results: str | None = None,
    ) -> ResearchResult | None: ...


@runtime_checkable
class ExecutionTracerProto(Protocol):
    """Protocol for execution trace recording (implemented in trace.py)."""

    def start_trace(
        self, task_id: str, initiated_by: str, user_request: str,
    ) -> ExecutionTrace: ...

    def record_step(
        self, trace: ExecutionTrace, step_result: StepResult,
    ) -> None: ...

    def record_quality_gate(
        self, trace: ExecutionTrace, gate_result: dict,
    ) -> None: ...

    async def finalize(self, trace: ExecutionTrace) -> str | None: ...
