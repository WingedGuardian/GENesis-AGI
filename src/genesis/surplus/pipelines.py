"""Surplus pipeline definitions — deterministic multi-step task chains.

A pipeline is a sequence of steps, each executed as a surplus task.
The dispatcher mechanically advances steps — the LLM never decides
whether the next step happens. Think factory conveyor belt: the worker
at station 1 doesn't decide if the part goes to station 2.

Pipeline state travels in the existing `payload` JSON field on
surplus_tasks (no schema migration needed).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

from genesis.surplus.types import ComputeTier, TaskType

logger = logging.getLogger(__name__)

# ── Payload key used to identify pipeline tasks ──────────────────────
PIPELINE_KEY = "pipeline"


@dataclass(frozen=True)
class PipelineStep:
    """One step in a surplus pipeline."""

    task_type: TaskType
    compute_tier: ComputeTier
    priority: float = 0.5


@dataclass(frozen=True)
class PipelineDefinition:
    """A named, deterministic sequence of surplus task steps."""

    name: str
    steps: tuple[PipelineStep, ...]
    drive_alignment: str
    description: str


# ── Pipeline registry ────────────────────────────────────────────────
# Add pipelines here as they are built. Each pipeline is a code-level
# definition — no DB config, no YAML. Adding a pipeline = adding an
# entry to this dict.

PIPELINES: dict[str, PipelineDefinition] = {
    "prompt_effectiveness": PipelineDefinition(
        name="prompt_effectiveness",
        steps=(
            PipelineStep(
                task_type=TaskType.PROMPT_REVIEW_CATALOG,
                compute_tier=ComputeTier.FREE_API,
                priority=0.4,
            ),
            PipelineStep(
                task_type=TaskType.PROMPT_REVIEW_SAMPLE,
                compute_tier=ComputeTier.FREE_API,
                priority=0.4,
            ),
            PipelineStep(
                task_type=TaskType.PROMPT_EFFECTIVENESS_REVIEW,
                compute_tier=ComputeTier.FREE_API,
                priority=0.5,
            ),
        ),
        drive_alignment="competence",
        description=(
            "Three-step prompt effectiveness review: "
            "catalog active call sites, sample recent outputs, "
            "evaluate gaps and recommend improvements"
        ),
    ),
}


# ── Payload helpers ──────────────────────────────────────────────────

def is_pipeline_task(payload: str | None) -> bool:
    """Return True if this task's payload marks it as a pipeline step."""
    if not payload:
        return False
    try:
        data = json.loads(payload)
        return PIPELINE_KEY in data
    except (json.JSONDecodeError, TypeError):
        return False


def parse_pipeline_payload(payload: str) -> dict:
    """Parse pipeline metadata from a task payload.

    Returns dict with keys: pipeline, pipeline_run_id, step, total_steps,
    and optionally previous_output.
    """
    return json.loads(payload)


def build_initial_payload(pipeline_name: str, total_steps: int) -> str:
    """Build the payload JSON for step 1 of a pipeline."""
    return json.dumps({
        PIPELINE_KEY: pipeline_name,
        "pipeline_run_id": str(uuid.uuid4()),
        "step": 1,
        "total_steps": total_steps,
    })


def build_next_step_payload(
    current_payload: dict,
    step_output: str,
) -> str:
    """Build the payload for the next step, carrying forward the previous output."""
    return json.dumps({
        PIPELINE_KEY: current_payload[PIPELINE_KEY],
        "pipeline_run_id": current_payload["pipeline_run_id"],
        "step": current_payload["step"] + 1,
        "total_steps": current_payload["total_steps"],
        "previous_output": step_output[:8000],  # Cap to prevent payload bloat
    })


def get_pipeline(name: str) -> PipelineDefinition | None:
    """Look up a pipeline definition by name."""
    return PIPELINES.get(name)
