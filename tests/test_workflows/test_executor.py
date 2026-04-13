"""Tests for workflow DAG executor."""

from genesis.workflows.executor import (
    advance_step,
    execute_deterministic_step,
    format_status,
    init_state,
    load_workflow,
    next_steps,
    validate_workflow,
)
from genesis.workflows.types import (
    StepType,
    WorkflowDef,
    WorkflowStep,
)


def test_load_workflow(tmp_path):
    """Load a workflow from YAML."""
    yaml_content = """\
name: test-flow
description: A test workflow
triggers: [test]
steps:
  - id: step1
    type: ai
    description: First step
  - id: step2
    type: deterministic
    command: "echo hello"
    depends_on: [step1]
"""
    path = tmp_path / "test.yaml"
    path.write_text(yaml_content)
    wf = load_workflow(path)
    assert wf.name == "test-flow"
    assert len(wf.steps) == 2
    assert wf.steps[0].step_type == StepType.AI
    assert wf.steps[1].depends_on == ["step1"]


def test_next_steps_respects_deps():
    """Only steps with met dependencies should be ready."""
    wf = WorkflowDef(
        name="test",
        steps=[
            WorkflowStep(id="a", step_type=StepType.AI),
            WorkflowStep(id="b", step_type=StepType.AI, depends_on=["a"]),
            WorkflowStep(id="c", step_type=StepType.AI, depends_on=["b"]),
        ],
    )
    state = init_state(wf)

    # Only "a" should be ready initially
    ready = next_steps(state)
    assert ready == ["a"]

    # Complete "a" -> "b" becomes ready
    advance_step(state, "a", success=True)
    ready = next_steps(state)
    assert ready == ["b"]

    # Complete "b" -> "c" becomes ready
    advance_step(state, "b", success=True)
    ready = next_steps(state)
    assert ready == ["c"]


def test_validate_cycle_detection():
    """Detect cycles in workflow dependencies."""
    wf = WorkflowDef(
        name="cycle",
        steps=[
            WorkflowStep(id="a", step_type=StepType.AI, depends_on=["b"]),
            WorkflowStep(id="b", step_type=StepType.AI, depends_on=["a"]),
        ],
    )
    errors = validate_workflow(wf)
    assert any("ycle" in e for e in errors)


def test_validate_missing_command():
    """Deterministic step without command is an error."""
    wf = WorkflowDef(
        name="bad",
        steps=[WorkflowStep(id="x", step_type=StepType.DETERMINISTIC)],
    )
    errors = validate_workflow(wf)
    assert any("no command" in e.lower() for e in errors)


def test_execute_deterministic_step():
    """Run a simple bash command."""
    step = WorkflowStep(
        id="echo", step_type=StepType.DETERMINISTIC, command="echo hello"
    )
    success, output = execute_deterministic_step(step)
    assert success
    assert "hello" in output


def test_execute_deterministic_step_failure():
    """Failed command returns success=False."""
    step = WorkflowStep(
        id="fail", step_type=StepType.DETERMINISTIC, command="exit 1"
    )
    success, _output = execute_deterministic_step(step)
    assert not success


def test_workflow_completion():
    """Workflow is complete when all steps are done."""
    wf = WorkflowDef(
        name="simple",
        steps=[
            WorkflowStep(id="a", step_type=StepType.AI),
            WorkflowStep(id="b", step_type=StepType.AI, depends_on=["a"]),
        ],
    )
    state = init_state(wf)
    assert not state.is_complete
    advance_step(state, "a", success=True)
    assert not state.is_complete
    advance_step(state, "b", success=True)
    assert state.is_complete


def test_format_status():
    """Status formatting produces readable output."""
    wf = WorkflowDef(
        name="fmt",
        steps=[
            WorkflowStep(
                id="a", step_type=StepType.AI, description="Do thing"
            )
        ],
    )
    state = init_state(wf)
    output = format_status(state)
    assert "fmt" in output
    assert "Do thing" in output
