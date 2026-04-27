"""Tests for genesis.autonomy.executor.types."""

from __future__ import annotations

import pytest

from genesis.autonomy.executor.types import (
    VALID_TRANSITIONS,
    ExecutionTrace,
    InvalidTransitionError,
    StepResult,
    StepType,
    TaskPhase,
    validate_transition,
)


class TestTaskPhase:
    def test_all_phases_have_transition_entry(self) -> None:
        """Every phase must appear as a key in VALID_TRANSITIONS."""
        for phase in TaskPhase:
            assert phase in VALID_TRANSITIONS, f"{phase} missing from VALID_TRANSITIONS"

    def test_terminal_phases_have_no_transitions(self) -> None:
        for phase in (TaskPhase.COMPLETED, TaskPhase.FAILED, TaskPhase.CANCELLED):
            assert VALID_TRANSITIONS[phase] == set(), (
                f"Terminal phase {phase} should have no outgoing transitions"
            )

    def test_pending_can_transition_to_reviewing(self) -> None:
        assert TaskPhase.REVIEWING in VALID_TRANSITIONS[TaskPhase.PENDING]

    def test_executing_can_loop_to_executing(self) -> None:
        """Executing -> Executing is valid (next step in sequence)."""
        assert TaskPhase.EXECUTING in VALID_TRANSITIONS[TaskPhase.EXECUTING]

    def test_verifying_can_go_back_to_executing(self) -> None:
        """Review failed -> iterate."""
        assert TaskPhase.EXECUTING in VALID_TRANSITIONS[TaskPhase.VERIFYING]

    def test_blocked_can_resume_to_executing(self) -> None:
        assert TaskPhase.EXECUTING in VALID_TRANSITIONS[TaskPhase.BLOCKED]

    def test_paused_can_resume_to_executing(self) -> None:
        assert TaskPhase.EXECUTING in VALID_TRANSITIONS[TaskPhase.PAUSED]


class TestValidateTransition:
    def test_valid_transition_succeeds(self) -> None:
        validate_transition(TaskPhase.PENDING, TaskPhase.REVIEWING)

    def test_invalid_transition_raises(self) -> None:
        with pytest.raises(InvalidTransitionError, match="Invalid transition"):
            validate_transition(TaskPhase.COMPLETED, TaskPhase.EXECUTING)

    def test_error_message_includes_allowed(self) -> None:
        with pytest.raises(InvalidTransitionError, match="Allowed:"):
            validate_transition(TaskPhase.PENDING, TaskPhase.COMPLETED)


class TestStepType:
    def test_default_timeouts(self) -> None:
        assert StepType.CODE.default_timeout_s == 3600
        assert StepType.RESEARCH.default_timeout_s == 3600
        assert StepType.ANALYSIS.default_timeout_s == 1800
        assert StepType.SYNTHESIS.default_timeout_s == 1800
        assert StepType.VERIFICATION.default_timeout_s == 3600
        assert StepType.EXTERNAL.default_timeout_s == 3600

    def test_all_types_have_timeouts(self) -> None:
        for st in StepType:
            assert isinstance(st.default_timeout_s, int)
            assert st.default_timeout_s > 0


class TestStepResult:
    def test_defaults(self) -> None:
        r = StepResult(idx=0, status="completed", result="done")
        assert r.cost_usd == 0.0
        assert r.session_id is None
        assert r.model_used == ""
        assert r.duration_s == 0.0
        assert r.artifacts == []
        assert r.blocker_description is None

    def test_blocked_result(self) -> None:
        r = StepResult(
            idx=1,
            status="blocked",
            result="partial work",
            blocker_description="Need API key",
        )
        assert r.status == "blocked"
        assert r.blocker_description == "Need API key"


class TestExecutionTrace:
    def test_defaults(self) -> None:
        t = ExecutionTrace(
            task_id="t1",
            initiated_by="user",
            user_request="build X",
        )
        assert t.plan == []
        assert t.sub_agents == []
        assert t.quality_gate == {}
        assert t.total_cost_usd == 0.0
        assert t.step_results == []
        assert t.retrospective_id is None
