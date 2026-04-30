"""Tests for genesis.autonomy.executor.trace.ExecutionTracer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from genesis.autonomy.executor.trace import ExecutionTracer
from genesis.autonomy.executor.types import StepResult


@dataclass
class FakeRoutingResult:
    success: bool
    content: str | None = None
    error: str | None = None


@pytest.mark.asyncio
class TestExecutionTracer:
    def test_start_trace(self) -> None:
        tracer = ExecutionTracer()
        trace = tracer.start_trace("t-001", "user", "Build feature X")

        assert trace.task_id == "t-001"
        assert trace.initiated_by == "user"
        assert trace.user_request == "Build feature X"
        assert trace.step_results == []
        assert trace.total_cost_usd == 0.0

    def test_record_step(self) -> None:
        tracer = ExecutionTracer()
        trace = tracer.start_trace("t-001", "user", "task")

        step = StepResult(idx=0, status="completed", result="done", cost_usd=0.05)
        tracer.record_step(trace, step)

        assert len(trace.step_results) == 1
        assert trace.total_cost_usd == pytest.approx(0.05)

    def test_record_multiple_steps_accumulates_cost(self) -> None:
        tracer = ExecutionTracer()
        trace = tracer.start_trace("t-001", "user", "task")

        tracer.record_step(trace, StepResult(idx=0, status="completed", result="a", cost_usd=0.03))
        tracer.record_step(trace, StepResult(idx=1, status="completed", result="b", cost_usd=0.07))

        assert len(trace.step_results) == 2
        assert trace.total_cost_usd == pytest.approx(0.10)

    def test_record_quality_gate(self) -> None:
        tracer = ExecutionTracer()
        trace = tracer.start_trace("t-001", "user", "task")

        gate = {"passed": True, "fresh_eyes": "ok", "adversarial": "ok"}
        tracer.record_quality_gate(trace, gate)

        assert trace.quality_gate == gate

    async def test_finalize_stores_memory(self) -> None:
        memory_store = AsyncMock()
        memory_store.store = AsyncMock(return_value="mem-abc123")

        tracer = ExecutionTracer(memory_store=memory_store)
        trace = tracer.start_trace("t-001", "user", "Build X")
        tracer.record_step(
            trace, StepResult(idx=0, status="completed", result="done", cost_usd=0.05),
        )

        summary = await tracer.finalize(trace)

        assert summary is not None
        assert "t-001" in summary
        memory_store.store.assert_awaited_once()
        call_kwargs = memory_store.store.call_args
        assert call_kwargs.kwargs["source"] == "task_executor"
        assert call_kwargs.kwargs["memory_type"] == "episodic"
        assert trace.retrospective_id == "mem-abc123"

    async def test_finalize_no_memory_store(self) -> None:
        tracer = ExecutionTracer(memory_store=None)
        trace = tracer.start_trace("t-001", "user", "task")

        result = await tracer.finalize(trace)
        assert result is None

    async def test_finalize_exception_returns_none(self) -> None:
        memory_store = AsyncMock()
        memory_store.store = AsyncMock(side_effect=RuntimeError("storage down"))

        tracer = ExecutionTracer(memory_store=memory_store)
        trace = tracer.start_trace("t-001", "user", "task")

        result = await tracer.finalize(trace)
        assert result is None

    def test_build_summary_format(self) -> None:
        tracer = ExecutionTracer()
        trace = tracer.start_trace("t-001", "genesis", "Research topic")
        tracer.record_step(
            trace,
            StepResult(idx=0, status="completed", result="Found answer", cost_usd=0.02, duration_s=5.0),
        )
        tracer.record_step(
            trace,
            StepResult(idx=1, status="failed", result="Timeout", cost_usd=0.01, duration_s=30.0),
        )

        summary = tracer._build_summary(trace)

        assert "Task Execution Trace: t-001" in summary
        assert "genesis" in summary
        assert "Step 0: completed" in summary
        assert "Step 1: failed" in summary
        assert "$0.0300" in summary  # total cost


# ---------------------------------------------------------------------------
# Retrospective tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRetrospective:
    async def test_retrospective_extracts_procedures(self) -> None:
        """Retrospective stores new procedures from LLM output."""
        retro_response = json.dumps({
            "new_procedures": [
                {
                    "task_type": "api-endpoint-creation",
                    "principle": "Always check for existing similar endpoints first",
                    "steps": ["Search for existing endpoints", "Create new endpoint"],
                    "tools_used": ["Grep", "Write"],
                    "context_tags": ["api", "endpoint"],
                },
            ],
            "procedure_updates": [],
            "skill_observations": [],
        })

        router = AsyncMock()
        router.route_call = AsyncMock(
            return_value=FakeRoutingResult(success=True, content=retro_response),
        )

        memory_store = AsyncMock()
        memory_store.store = AsyncMock(return_value="mem-001")
        mock_db = AsyncMock()

        tracer = ExecutionTracer(
            db=mock_db, memory_store=memory_store, router=router,
        )
        trace = tracer.start_trace("t-retro", "user", "Build API endpoint")
        tracer.record_step(
            trace, StepResult(idx=0, status="completed", result="done", cost_usd=0.1),
        )

        with patch(
            "genesis.learning.procedural.operations.store_procedure",
            AsyncMock(return_value="proc-new-123"),
        ) as mock_store:
            summary = await tracer.finalize(trace)

        assert summary is not None
        mock_store.assert_awaited_once()
        call_kwargs = mock_store.call_args
        # First positional arg is db
        assert call_kwargs.kwargs["task_type"] == "api-endpoint-creation"
        assert call_kwargs.kwargs["activation_tier"] == "L4"
        assert call_kwargs.kwargs["speculative"] == 1
        assert "proc-new-123" in trace.procedural_extractions

    async def test_retrospective_skipped_without_router(self) -> None:
        """No router means no retrospective — finalize still succeeds."""
        memory_store = AsyncMock()
        memory_store.store = AsyncMock(return_value="mem-002")

        tracer = ExecutionTracer(memory_store=memory_store, router=None)
        trace = tracer.start_trace("t-no-router", "user", "task")
        tracer.record_step(
            trace, StepResult(idx=0, status="completed", result="ok", cost_usd=0.01),
        )

        summary = await tracer.finalize(trace)
        assert summary is not None
        assert trace.procedural_extractions == []

    async def test_retrospective_failure_creates_follow_up(self) -> None:
        """When retrospective LLM call fails, a follow-up is created."""
        router = AsyncMock()
        router.route_call = AsyncMock(
            return_value=FakeRoutingResult(success=False, content=None),
        )

        memory_store = AsyncMock()
        memory_store.store = AsyncMock(return_value="mem-003")

        mock_db = AsyncMock()

        tracer = ExecutionTracer(
            db=mock_db, memory_store=memory_store, router=router,
        )
        trace = tracer.start_trace("t-fail", "user", "task")
        tracer.record_step(
            trace, StepResult(idx=0, status="completed", result="ok", cost_usd=0.01),
        )

        with patch(
            "genesis.db.crud.follow_ups.create",
            AsyncMock(),
        ) as mock_fu:
            await tracer.finalize(trace)

        mock_fu.assert_awaited_once()
        call_kwargs = mock_fu.call_args
        assert "retrospective" in call_kwargs.kwargs.get("content", "").lower() or \
               "retrospective" in str(call_kwargs)

    async def test_retrospective_unparseable_json_creates_follow_up(self) -> None:
        """Unparseable LLM response triggers follow-up creation."""
        router = AsyncMock()
        router.route_call = AsyncMock(
            return_value=FakeRoutingResult(success=True, content="not json at all"),
        )

        memory_store = AsyncMock()
        memory_store.store = AsyncMock(return_value="mem-004")

        mock_db = AsyncMock()

        tracer = ExecutionTracer(
            db=mock_db, memory_store=memory_store, router=router,
        )
        trace = tracer.start_trace("t-bad-json", "user", "task")
        tracer.record_step(
            trace, StepResult(idx=0, status="completed", result="ok", cost_usd=0.01),
        )

        with patch(
            "genesis.db.crud.follow_ups.create",
            AsyncMock(),
        ) as mock_fu:
            await tracer.finalize(trace)

        mock_fu.assert_awaited_once()

    async def test_retrospective_empty_extractions(self) -> None:
        """Empty extraction arrays are handled gracefully."""
        retro_response = json.dumps({
            "new_procedures": [],
            "procedure_updates": [],
            "skill_observations": [],
        })

        router = AsyncMock()
        router.route_call = AsyncMock(
            return_value=FakeRoutingResult(success=True, content=retro_response),
        )

        memory_store = AsyncMock()
        memory_store.store = AsyncMock(return_value="mem-005")

        tracer = ExecutionTracer(memory_store=memory_store, router=router)
        trace = tracer.start_trace("t-empty", "user", "task")
        tracer.record_step(
            trace, StepResult(idx=0, status="completed", result="ok", cost_usd=0.01),
        )

        summary = await tracer.finalize(trace)
        assert summary is not None
        assert trace.procedural_extractions == []

    async def test_retrospective_caps_procedures(self) -> None:
        """Max 3 procedures extracted per task."""
        procs = [
            {
                "task_type": f"proc-{i}",
                "principle": f"Procedure {i}",
                "steps": [f"step {i}"],
                "tools_used": ["Bash"],
                "context_tags": [f"tag{i}"],
            }
            for i in range(5)  # 5 returned, only 3 stored
        ]
        retro_response = json.dumps({
            "new_procedures": procs,
            "procedure_updates": [],
            "skill_observations": [],
        })

        router = AsyncMock()
        router.route_call = AsyncMock(
            return_value=FakeRoutingResult(success=True, content=retro_response),
        )

        memory_store = AsyncMock()
        memory_store.store = AsyncMock(return_value="mem-006")

        mock_db = AsyncMock()

        tracer = ExecutionTracer(db=mock_db, memory_store=memory_store, router=router)
        trace = tracer.start_trace("t-cap", "user", "task")
        tracer.record_step(
            trace, StepResult(idx=0, status="completed", result="ok", cost_usd=0.01),
        )

        store_calls = []

        async def fake_store(db, **kwargs):
            pid = f"proc-id-{len(store_calls)}"
            store_calls.append(kwargs)
            return pid

        with patch(
            "genesis.learning.procedural.operations.store_procedure",
            side_effect=fake_store,
        ):
            await tracer.finalize(trace)

        assert len(store_calls) == 3  # Capped at _MAX_NEW_PROCEDURES
        assert len(trace.procedural_extractions) == 3

    async def test_skill_observation_stored(self) -> None:
        """Skill observations are stored via memory_store."""
        retro_response = json.dumps({
            "new_procedures": [],
            "procedure_updates": [],
            "skill_observations": [
                {"skill_name": "research", "observation": "Add timeout handling"},
            ],
        })

        router = AsyncMock()
        router.route_call = AsyncMock(
            return_value=FakeRoutingResult(success=True, content=retro_response),
        )

        memory_store = AsyncMock()
        memory_store.store = AsyncMock(return_value="mem-007")

        tracer = ExecutionTracer(memory_store=memory_store, router=router)
        trace = tracer.start_trace("t-skill", "user", "research task")
        tracer.record_step(
            trace, StepResult(idx=0, status="completed", result="ok", cost_usd=0.01),
        )

        await tracer.finalize(trace)

        # memory_store.store called at least twice: episodic trace + skill observation
        assert memory_store.store.await_count >= 2
        # Find the skill observation call
        skill_call = None
        for call in memory_store.store.call_args_list:
            tags = call.kwargs.get("tags", [])
            if "skill_update_candidate" in tags:
                skill_call = call
                break
        assert skill_call is not None
        assert "research" in skill_call.kwargs["tags"]
