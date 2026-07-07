"""Direct tests for the surplus dispatch engine (genesis.surplus.dispatch).

The pipeline was decomposed from SurplusScheduler.dispatch_once into named
phases; this locks its behavior at its own boundary — independent of the
scheduler facade — so it can evolve toward v4-parallel-dispatch without silent
drift. Focus: the failure asymmetry (only the executor-exception path emits
task.failed), executor-selection fallback semantics, the maintenance→idle→
task ordering, and the consecutive-failure observation threshold.
"""

import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import genesis.db.crud.observations as observations_mod
import genesis.db.crud.surplus_tasks as surplus_tasks_mod
from genesis.surplus import dispatch
from genesis.surplus.types import TaskType


class _Task:
    def __init__(self, task_type=TaskType.DISK_CLEANUP, tid="task-1"):
        self.id = tid
        self.task_type = task_type
        self.drive_alignment = "competence"
        self.payload = None


class _Result:
    def __init__(self, success=True, error=None, insights=None, content=""):
        self.success = success
        self.error = error
        self.insights = insights or []
        self.content = content


def _make_ctx(*, executor=None, executors=None, idle=True):
    ctx = types.SimpleNamespace()
    ctx._db = object()
    ctx._event_bus = AsyncMock()
    ctx._queue = AsyncMock()
    ctx._idle_detector = types.SimpleNamespace(is_idle=lambda: idle)
    ctx._compute = types.SimpleNamespace(
        get_available_tiers=AsyncMock(return_value=["free_api"])
    )
    ctx._executor = executor if executor is not None else AsyncMock()
    ctx._executors = executors or {}
    ctx._judge_router = None
    ctx._clock = lambda: datetime.now(UTC)
    ctx._task_expiry_hours = 72
    ctx._terminal_retention_days = 30
    return ctx


# ── _select_executor ────────────────────────────────────────────────

def test_select_executor_registry_hit():
    dedicated = object()
    ctx = _make_ctx(executors={TaskType.CODE_AUDIT: dedicated})
    assert dispatch._select_executor(ctx, _Task(TaskType.CODE_AUDIT)) is dedicated


def test_select_executor_falls_back_when_unregistered():
    default = object()
    ctx = _make_ctx(executor=default)
    assert dispatch._select_executor(ctx, _Task(TaskType.DISK_CLEANUP)) is default


def test_select_executor_falls_back_when_registered_none():
    # A stored None must still fall back to the default (not dict.get default).
    default = object()
    ctx = _make_ctx(executor=default, executors={TaskType.DISK_CLEANUP: None})
    assert dispatch._select_executor(ctx, _Task(TaskType.DISK_CLEANUP)) is default


# ── dispatch_once: the failure asymmetry ────────────────────────────

async def test_executor_exception_marks_failed_and_emits_task_failed():
    executor = types.SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("boom"))
    )
    ctx = _make_ctx(executor=executor)
    ctx._queue.next_task = AsyncMock(return_value=_Task())

    result = await dispatch.dispatch_once(ctx)

    assert result is False
    ctx._queue.mark_failed.assert_awaited_once()
    assert ctx._queue.mark_failed.await_args.kwargs["reason"] == "executor_exception"
    # Asymmetry: the executor-exception path emits exactly one task.failed event.
    assert ctx._event_bus.emit.await_count == 1
    assert "task.failed" in ctx._event_bus.emit.await_args.args


async def test_result_failure_marks_failed_but_does_not_emit():
    executor = types.SimpleNamespace(
        execute=AsyncMock(return_value=_Result(success=False, error="nope"))
    )
    ctx = _make_ctx(executor=executor)
    ctx._queue.next_task = AsyncMock(return_value=_Task())

    result = await dispatch.dispatch_once(ctx)

    assert result is False
    ctx._queue.mark_failed.assert_awaited_once()
    assert ctx._queue.mark_failed.await_args.kwargs["reason"] == "nope"
    # Asymmetry: the result-failure path does NOT emit task.failed.
    ctx._event_bus.emit.assert_not_awaited()


# ── dispatch_once: control-flow guards ──────────────────────────────

async def test_not_idle_runs_maintenance_then_short_circuits():
    ctx = _make_ctx(idle=False)

    result = await dispatch.dispatch_once(ctx)

    assert result is False
    # Maintenance sweep runs unconditionally, before the idle gate.
    ctx._queue.recover_stuck.assert_awaited_once()
    ctx._queue.drain_expired.assert_awaited_once()
    ctx._queue.reap_terminal.assert_awaited_once()
    # Idle gate short-circuits before task selection.
    ctx._queue.next_task.assert_not_awaited()


async def test_no_task_returns_false():
    ctx = _make_ctx()
    ctx._queue.next_task = AsyncMock(return_value=None)

    assert await dispatch.dispatch_once(ctx) is False
    ctx._queue.mark_running.assert_not_awaited()


async def test_success_path_marks_completed_and_returns_true():
    executor = types.SimpleNamespace(
        execute=AsyncMock(return_value=_Result(success=True, insights=[], content=""))
    )
    ctx = _make_ctx(executor=executor)
    ctx._queue.next_task = AsyncMock(return_value=_Task())

    result = await dispatch.dispatch_once(ctx)

    assert result is True
    ctx._queue.mark_running.assert_awaited_once()
    ctx._queue.mark_completed.assert_awaited_once()
    ctx._queue.mark_failed.assert_not_awaited()
    ctx._event_bus.emit.assert_not_awaited()


# ── maybe_observe_failure: the 3-strike threshold ───────────────────

async def test_maybe_observe_failure_upserts_at_threshold(monkeypatch):
    ctx = _make_ctx()
    upsert_spy = AsyncMock()
    monkeypatch.setattr(
        surplus_tasks_mod, "consecutive_failures", AsyncMock(return_value=3)
    )
    monkeypatch.setattr(observations_mod, "upsert", upsert_spy)

    await dispatch.maybe_observe_failure(ctx, _Task(), "some reason")

    upsert_spy.assert_awaited_once()
    assert upsert_spy.await_args.kwargs["type"] == "surplus_task_failing"


async def test_maybe_observe_failure_silent_below_threshold(monkeypatch):
    ctx = _make_ctx()
    upsert_spy = AsyncMock()
    monkeypatch.setattr(
        surplus_tasks_mod, "consecutive_failures", AsyncMock(return_value=2)
    )
    monkeypatch.setattr(observations_mod, "upsert", upsert_spy)

    await dispatch.maybe_observe_failure(ctx, _Task(), "some reason")

    upsert_spy.assert_not_awaited()
