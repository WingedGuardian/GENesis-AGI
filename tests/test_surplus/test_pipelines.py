"""Tests for surplus pipeline infrastructure — registry, payload helpers, chaining."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from genesis.surplus.pipelines import (
    PIPELINE_KEY,
    PIPELINES,
    PipelineDefinition,
    PipelineStep,
    build_initial_payload,
    build_next_step_payload,
    get_pipeline,
    is_pipeline_task,
    parse_pipeline_payload,
)
from genesis.surplus.types import ComputeTier, TaskType

# ── Test pipeline definition for tests only ─────────────────────────

_TEST_PIPELINE = PipelineDefinition(
    name="_test_two_step",
    steps=(
        PipelineStep(
            task_type=TaskType.GAP_CLUSTERING,
            compute_tier=ComputeTier.FREE_API,
            priority=0.4,
        ),
        PipelineStep(
            task_type=TaskType.BRAINSTORM_SELF,
            compute_tier=ComputeTier.FREE_API,
            priority=0.5,
        ),
    ),
    drive_alignment="competence",
    description="Test pipeline with two steps",
)


@pytest.fixture(autouse=True)
def _register_test_pipeline():
    """Register test pipeline for the duration of each test."""
    PIPELINES["_test_two_step"] = _TEST_PIPELINE
    yield
    PIPELINES.pop("_test_two_step", None)


# ── Registry tests ──────────────────────────────────────────────────

def test_get_pipeline_found():
    assert get_pipeline("_test_two_step") is _TEST_PIPELINE


def test_get_pipeline_missing():
    assert get_pipeline("nonexistent") is None


# ── Payload helper tests ────────────────────────────────────────────

def test_is_pipeline_task_true():
    payload = json.dumps({PIPELINE_KEY: "test", "step": 1})
    assert is_pipeline_task(payload) is True


def test_is_pipeline_task_false_no_key():
    payload = json.dumps({"source": "follow_up"})
    assert is_pipeline_task(payload) is False


def test_is_pipeline_task_none():
    assert is_pipeline_task(None) is False


def test_is_pipeline_task_empty_string():
    assert is_pipeline_task("") is False


def test_is_pipeline_task_invalid_json():
    assert is_pipeline_task("not json") is False


def test_build_initial_payload():
    payload = build_initial_payload("_test_two_step", 2)
    data = json.loads(payload)
    assert data[PIPELINE_KEY] == "_test_two_step"
    assert data["step"] == 1
    assert data["total_steps"] == 2
    assert "pipeline_run_id" in data


def test_build_next_step_payload():
    current = {
        PIPELINE_KEY: "_test_two_step",
        "pipeline_run_id": "abc-123",
        "step": 1,
        "total_steps": 2,
    }
    payload = build_next_step_payload(current, "step 1 output text")
    data = json.loads(payload)
    assert data[PIPELINE_KEY] == "_test_two_step"
    assert data["pipeline_run_id"] == "abc-123"
    assert data["step"] == 2
    assert data["total_steps"] == 2
    assert data["previous_output"] == "step 1 output text"


def test_build_next_step_payload_caps_output():
    """Previous output should be capped to prevent payload bloat."""
    current = {
        PIPELINE_KEY: "_test_two_step",
        "pipeline_run_id": "abc-123",
        "step": 1,
        "total_steps": 3,
    }
    long_output = "x" * 10000
    payload = build_next_step_payload(current, long_output)
    data = json.loads(payload)
    assert len(data["previous_output"]) == 8000


def test_parse_pipeline_payload():
    payload = json.dumps({
        PIPELINE_KEY: "test",
        "step": 2,
        "total_steps": 3,
        "previous_output": "hello",
    })
    data = parse_pipeline_payload(payload)
    assert data["step"] == 2
    assert data["previous_output"] == "hello"


# ── Integration: chaining in dispatch_once ──────────────────────────
# Uses the global `db` fixture from conftest.py (in-memory SQLite with
# all tables created and seeded).


async def test_pipeline_chaining_enqueues_next_step(db):
    """When step 1 of a 2-step pipeline completes, step 2 is enqueued."""
    from genesis.surplus.queue import SurplusQueue
    from genesis.surplus.scheduler import SurplusScheduler
    from genesis.surplus.types import ExecutorResult

    queue = SurplusQueue(db=db)

    # Create a mock executor that returns success
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ExecutorResult(
        success=True,
        content="Step 1 analysis results here.",
        insights=[{"generating_model": "test", "confidence": 0.5}],
    )

    sched = SurplusScheduler(
        db=db,
        queue=queue,
        idle_detector=AsyncMock(),
        compute_availability=AsyncMock(),
        executor=mock_executor,
    )
    # Make idle detector say we're idle
    sched._idle_detector.is_idle = lambda **kwargs: True

    # Enqueue step 1 of test pipeline
    payload = build_initial_payload("_test_two_step", 2)
    await queue.enqueue(
        TaskType.GAP_CLUSTERING, ComputeTier.FREE_API,
        0.4, "competence", payload=payload,
    )

    # Mock compute availability to return FREE_API
    with patch.object(sched._compute, "get_available_tiers", new_callable=AsyncMock, return_value=[ComputeTier.FREE_API]):
        dispatched = await sched.dispatch_once()

    assert dispatched is True

    # Step 2 should now be pending
    pending = await queue.pending_by_type(TaskType.BRAINSTORM_SELF)
    assert pending == 1

    # Verify step 2 payload contains previous output
    task2 = await queue.next_task([ComputeTier.FREE_API])
    assert task2 is not None
    data = json.loads(task2.payload)
    assert data["step"] == 2
    assert data["total_steps"] == 2
    assert "Step 1 analysis results" in data["previous_output"]


async def test_pipeline_final_step_no_enqueue(db):
    """Final step of a pipeline does NOT enqueue another step."""
    from genesis.surplus.queue import SurplusQueue
    from genesis.surplus.scheduler import SurplusScheduler
    from genesis.surplus.types import ExecutorResult

    queue = SurplusQueue(db=db)
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ExecutorResult(
        success=True,
        content="Final step output.",
        insights=[{"generating_model": "test", "confidence": 0.5}],
    )

    sched = SurplusScheduler(
        db=db, queue=queue,
        idle_detector=AsyncMock(),
        compute_availability=AsyncMock(),
        executor=mock_executor,
    )
    sched._idle_detector.is_idle = lambda **kwargs: True

    # Enqueue step 2 of 2 (final step)
    payload = json.dumps({
        PIPELINE_KEY: "_test_two_step",
        "pipeline_run_id": "run-final",
        "step": 2,
        "total_steps": 2,
        "previous_output": "step 1 output",
    })
    await queue.enqueue(
        TaskType.BRAINSTORM_SELF, ComputeTier.FREE_API,
        0.5, "competence", payload=payload,
    )

    with patch.object(sched._compute, "get_available_tiers", new_callable=AsyncMock, return_value=[ComputeTier.FREE_API]):
        await sched.dispatch_once()

    # No new tasks should be pending
    total = await queue.pending_count()
    assert total == 0


async def test_non_pipeline_task_no_chaining(db):
    """Regular (non-pipeline) tasks don't trigger chaining."""
    from genesis.surplus.queue import SurplusQueue
    from genesis.surplus.scheduler import SurplusScheduler
    from genesis.surplus.types import ExecutorResult

    queue = SurplusQueue(db=db)
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ExecutorResult(
        success=True,
        content="Regular task output that is long enough to pass quality gate easily.",
        insights=[{"generating_model": "test", "confidence": 0.5}],
    )

    sched = SurplusScheduler(
        db=db, queue=queue,
        idle_detector=AsyncMock(),
        compute_availability=AsyncMock(),
        executor=mock_executor,
    )
    sched._idle_detector.is_idle = lambda **kwargs: True

    # Enqueue a regular task (no pipeline payload)
    await queue.enqueue(
        TaskType.GAP_CLUSTERING, ComputeTier.FREE_API,
        0.4, "competence",
    )

    with patch.object(sched._compute, "get_available_tiers", new_callable=AsyncMock, return_value=[ComputeTier.FREE_API]):
        await sched.dispatch_once()

    # No chained tasks
    total = await queue.pending_count()
    assert total == 0


async def test_malformed_pipeline_payload_no_crash(db):
    """Malformed pipeline payload doesn't crash the dispatcher."""
    from genesis.surplus.queue import SurplusQueue
    from genesis.surplus.scheduler import SurplusScheduler
    from genesis.surplus.types import ExecutorResult

    queue = SurplusQueue(db=db)
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ExecutorResult(
        success=True,
        content="Output from task with bad payload that is long enough to pass.",
        insights=[{"generating_model": "test", "confidence": 0.5}],
    )

    sched = SurplusScheduler(
        db=db, queue=queue,
        idle_detector=AsyncMock(),
        compute_availability=AsyncMock(),
        executor=mock_executor,
    )
    sched._idle_detector.is_idle = lambda **kwargs: True

    # Enqueue task with malformed pipeline payload
    await queue.enqueue(
        TaskType.GAP_CLUSTERING, ComputeTier.FREE_API,
        0.4, "competence",
        payload=json.dumps({PIPELINE_KEY: "nonexistent", "step": 1, "total_steps": 2}),
    )

    with patch.object(sched._compute, "get_available_tiers", new_callable=AsyncMock, return_value=[ComputeTier.FREE_API]):
        # Should not raise
        dispatched = await sched.dispatch_once()

    assert dispatched is True


async def test_schedule_pipeline(db):
    """schedule_pipeline enqueues step 1 and returns task ID."""
    from genesis.surplus.queue import SurplusQueue
    from genesis.surplus.scheduler import SurplusScheduler

    queue = SurplusQueue(db=db)
    sched = SurplusScheduler(
        db=db, queue=queue,
        idle_detector=AsyncMock(),
        compute_availability=AsyncMock(),
    )

    task_id = await sched.schedule_pipeline("_test_two_step")
    assert task_id is not None

    # Step 1 task type should be pending
    pending = await queue.pending_by_type(TaskType.GAP_CLUSTERING)
    assert pending == 1


async def test_schedule_pipeline_skips_if_pending(db):
    """schedule_pipeline skips if step 1 task type already pending."""
    from genesis.surplus.queue import SurplusQueue
    from genesis.surplus.scheduler import SurplusScheduler

    queue = SurplusQueue(db=db)
    sched = SurplusScheduler(
        db=db, queue=queue,
        idle_detector=AsyncMock(),
        compute_availability=AsyncMock(),
    )

    # First call succeeds
    await sched.schedule_pipeline("_test_two_step")
    # Second call returns None (already pending)
    result = await sched.schedule_pipeline("_test_two_step")
    assert result is None

    # Only 1 task pending, not 2
    pending = await queue.pending_by_type(TaskType.GAP_CLUSTERING)
    assert pending == 1


async def test_schedule_pipeline_unknown():
    """schedule_pipeline returns None for unknown pipeline name."""
    from genesis.surplus.scheduler import SurplusScheduler

    sched = SurplusScheduler(
        db=AsyncMock(), queue=AsyncMock(),
        idle_detector=AsyncMock(),
        compute_availability=AsyncMock(),
    )
    result = await sched.schedule_pipeline("nonexistent_pipeline")
    assert result is None
