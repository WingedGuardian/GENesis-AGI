"""Tests for genesis.autonomy.executor.trace.ExecutionTracer."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.autonomy.executor.trace import ExecutionTracer
from genesis.autonomy.executor.types import StepResult


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
