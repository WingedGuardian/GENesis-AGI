"""Workflow type definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class StepType(StrEnum):
    AI = "ai"
    DETERMINISTIC = "deterministic"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class WorkflowStep:
    """A single step in a workflow DAG."""

    id: str
    step_type: StepType
    description: str = ""
    prompt: str | None = None  # For AI steps
    command: str | None = None  # For deterministic steps
    depends_on: list[str] = field(default_factory=list)
    gate: str | None = None  # Gate condition (free-form, checked by executor)
    optional: bool = False


@dataclass
class StepState:
    """Mutable runtime state for a step."""

    step: WorkflowStep
    status: StepStatus = StepStatus.PENDING
    output: str | None = None
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True)
class WorkflowDef:
    """A complete workflow definition loaded from YAML."""

    name: str
    description: str = ""
    triggers: list[str] = field(default_factory=list)
    steps: list[WorkflowStep] = field(default_factory=list)


@dataclass
class WorkflowState:
    """Runtime state of an executing workflow."""

    definition: WorkflowDef
    step_states: dict[str, StepState] = field(default_factory=dict)
    current_step_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    @property
    def is_complete(self) -> bool:
        return all(
            s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED)
            for s in self.step_states.values()
        )

    @property
    def is_failed(self) -> bool:
        return any(
            s.status == StepStatus.FAILED and not s.step.optional
            for s in self.step_states.values()
        )
