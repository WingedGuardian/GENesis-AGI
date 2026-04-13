"""Workflow DAG executor -- parses YAML, tracks state, enforces gates."""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml

from genesis.workflows.types import (
    StepState,
    StepStatus,
    StepType,
    WorkflowDef,
    WorkflowState,
    WorkflowStep,
)

logger = logging.getLogger(__name__)

# Default workflow directory
WORKFLOW_DIR = Path.home() / ".genesis" / "workflows"


def load_workflow(path: Path) -> WorkflowDef:
    """Load a workflow definition from a YAML file."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"Invalid workflow YAML: expected dict, got {type(raw).__name__}"
        raise ValueError(msg)

    steps = []
    for step_raw in raw.get("steps", []):
        steps.append(
            WorkflowStep(
                id=step_raw["id"],
                step_type=StepType(step_raw.get("type", "ai")),
                description=step_raw.get("description", ""),
                prompt=step_raw.get("prompt"),
                command=step_raw.get("command"),
                depends_on=step_raw.get("depends_on", []),
                gate=step_raw.get("gate"),
                optional=step_raw.get("optional", False),
            )
        )

    return WorkflowDef(
        name=raw.get("name", path.stem),
        description=raw.get("description", ""),
        triggers=raw.get("triggers", []),
        steps=steps,
    )


def load_all_workflows(
    workflow_dir: Path = WORKFLOW_DIR,
) -> dict[str, WorkflowDef]:
    """Load all .yaml workflow definitions from directory."""
    workflows: dict[str, WorkflowDef] = {}
    if not workflow_dir.is_dir():
        return workflows
    for path in sorted(workflow_dir.glob("*.yaml")):
        try:
            wf = load_workflow(path)
            workflows[wf.name] = wf
        except Exception:
            logger.warning("Failed to load workflow: %s", path, exc_info=True)
    return workflows


def init_state(definition: WorkflowDef) -> WorkflowState:
    """Create initial runtime state for a workflow."""
    state = WorkflowState(
        definition=definition,
        started_at=datetime.now(UTC).isoformat(),
    )
    for step in definition.steps:
        state.step_states[step.id] = StepState(step=step)
    return state


def next_steps(state: WorkflowState) -> list[str]:
    """Return step IDs that are ready to execute (all dependencies met)."""
    ready = []
    for step_id, step_state in state.step_states.items():
        if step_state.status != StepStatus.PENDING:
            continue
        # Check all dependencies are completed
        deps_met = all(
            state.step_states[dep].status
            in (StepStatus.COMPLETED, StepStatus.SKIPPED)
            for dep in step_state.step.depends_on
            if dep in state.step_states
        )
        if deps_met:
            ready.append(step_id)
    return ready


def validate_workflow(definition: WorkflowDef) -> list[str]:
    """Validate a workflow definition. Returns list of error messages (empty = valid)."""
    errors: list[str] = []
    step_ids = {s.id for s in definition.steps}

    # Check for duplicate IDs
    seen: set[str] = set()
    for step in definition.steps:
        if step.id in seen:
            errors.append(f"Duplicate step ID: {step.id}")
        seen.add(step.id)

    # Check dependency references
    for step in definition.steps:
        for dep in step.depends_on:
            if dep not in step_ids:
                errors.append(
                    f"Step '{step.id}' depends on unknown step '{dep}'"
                )

    # Check for cycles (simple DFS)
    visited: set[str] = set()
    path_set: set[str] = set()

    def _has_cycle(step_id: str) -> bool:
        if step_id in path_set:
            return True
        if step_id in visited:
            return False
        visited.add(step_id)
        path_set.add(step_id)
        step = next((s for s in definition.steps if s.id == step_id), None)
        if step:
            for dep in step.depends_on:
                if _has_cycle(dep):
                    return True
        path_set.discard(step_id)
        return False

    for step in definition.steps:
        if _has_cycle(step.id):
            errors.append(f"Cycle detected involving step '{step.id}'")
            break

    # Check deterministic steps have commands
    for step in definition.steps:
        if step.step_type == StepType.DETERMINISTIC and not step.command:
            errors.append(f"Deterministic step '{step.id}' has no command")

    return errors


def execute_deterministic_step(
    step: WorkflowStep,
    *,
    cwd: str | None = None,
) -> tuple[bool, str]:
    """Execute a deterministic (bash) step. Returns (success, output).

    SECURITY: Commands come from trusted YAML files in ~/.genesis/workflows/.
    Only the user or Genesis (with user-granted autonomy) can place files there.
    shell=True is intentional — workflow commands use shell features (&&, |, ~).
    """
    # GROUNDWORK(workflow-sandboxing): Future: validate commands against allowlist
    if not step.command:
        return False, "No command specified"
    try:
        result = subprocess.run(
            step.command,
            shell=True,  # noqa: S602 — trusted YAML source, see docstring
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=300,  # user-acknowledged: workflow steps need bounded execution
        )
        output = result.stdout
        if result.returncode != 0:
            output = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            return False, output
        return True, output
    except subprocess.TimeoutExpired:
        return False, "Command timed out (300s)"
    except Exception as exc:
        return False, str(exc)


def advance_step(
    state: WorkflowState,
    step_id: str,
    *,
    success: bool,
    output: str = "",
) -> None:
    """Mark a step as completed or failed and update state."""
    step_state = state.step_states.get(step_id)
    if step_state is None:
        msg = f"Unknown step: {step_id}"
        raise ValueError(msg)
    now = datetime.now(UTC).isoformat()
    if not step_state.started_at:
        step_state.started_at = now
    step_state.completed_at = now
    step_state.output = output
    if success:
        step_state.status = StepStatus.COMPLETED
    else:
        step_state.status = StepStatus.FAILED
        step_state.error = output

    # Check if workflow is complete
    if state.is_complete or state.is_failed:
        state.completed_at = now


def format_status(state: WorkflowState) -> str:
    """Format workflow state as human-readable status."""
    lines = [f"## Workflow: {state.definition.name}"]
    for step_state in state.step_states.values():
        icon = {
            StepStatus.PENDING: "[ ]",
            StepStatus.RUNNING: "[~]",
            StepStatus.COMPLETED: "[x]",
            StepStatus.FAILED: "[!]",
            StepStatus.SKIPPED: "[-]",
        }.get(step_state.status, "[?]")
        desc = step_state.step.description or step_state.step.id
        gate = f" (gate: {step_state.step.gate})" if step_state.step.gate else ""
        lines.append(f"{icon} {desc}{gate}")
    return "\n".join(lines)
