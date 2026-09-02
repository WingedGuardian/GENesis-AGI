"""Direct tests for the surplus dispatch engine (genesis.surplus.dispatch).

The pipeline was decomposed from SurplusScheduler.dispatch_once into named
phases; this locks its behavior at its own boundary — independent of the
scheduler facade — so it can evolve toward v4-parallel-dispatch without silent
drift. Focus: the failure asymmetry (only the executor-exception path emits
task.failed), executor-selection fallback semantics, the maintenance→idle→
task ordering, and the consecutive-failure observation threshold.
"""

import logging
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import genesis.db.crud.observations as observations_mod
import genesis.db.crud.surplus_tasks as surplus_tasks_mod
from genesis.resilience import network_config
from genesis.surplus import dispatch
from genesis.surplus.types import ComputeTier, TaskType


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


# ── dispatch_once: heavy_workload flag guards the zombie watchdog ────
# A single long dispatch (e.g. j9_eval_batch ~25 min) must set the runtime
# heavy_workload flag for the DURATION of executor.execute so the 900s
# zombie-scheduler watchdog forgives it instead of restarting the server.

async def test_heavy_workload_flag_set_during_execute_and_cleared_after(monkeypatch):
    import genesis.runtime as runtime_mod

    fake_rt = types.SimpleNamespace(_heavy_workload=None, _heavy_workload_since=None)
    monkeypatch.setattr(
        runtime_mod.GenesisRuntime, "instance", staticmethod(lambda: fake_rt)
    )

    task = _Task(TaskType.J9_EVAL_BATCH)
    captured = {}

    async def _capture_execute(t):
        # Observe the flag AT execute time — this is the window the watchdog sees.
        captured["hw"] = fake_rt._heavy_workload
        captured["since"] = fake_rt._heavy_workload_since
        return _Result(success=True)

    ctx = _make_ctx(executor=types.SimpleNamespace(execute=_capture_execute))
    ctx._queue.next_task = AsyncMock(return_value=task)

    result = await dispatch.dispatch_once(ctx)

    assert result is True
    # DURING execute the flag was set to the generic cycle label (set at entry).
    assert captured["hw"] == "surplus:dispatch"
    assert captured["since"] is not None
    # AFTER the dispatch the flag is cleared (label-guarded) — no leak.
    assert fake_rt._heavy_workload is None
    assert fake_rt._heavy_workload_since is None


async def test_heavy_workload_flag_cleared_even_when_executor_raises(monkeypatch):
    import genesis.runtime as runtime_mod

    fake_rt = types.SimpleNamespace(_heavy_workload=None, _heavy_workload_since=None)
    monkeypatch.setattr(
        runtime_mod.GenesisRuntime, "instance", staticmethod(lambda: fake_rt)
    )

    seen = {}

    async def _boom(_t):
        seen["hw"] = fake_rt._heavy_workload  # set at entry, before execute
        raise RuntimeError("hang→boom")

    task = _Task(TaskType.J9_EVAL_BATCH)
    ctx = _make_ctx(executor=types.SimpleNamespace(execute=_boom))
    ctx._queue.next_task = AsyncMock(return_value=task)

    result = await dispatch.dispatch_once(ctx)

    assert result is False
    # Flag was set at ENTRY, before the executor ran.
    assert seen["hw"] == "surplus:dispatch"
    # The finally clears it even on the exception path — no stuck heavy flag.
    assert fake_rt._heavy_workload is None
    assert fake_rt._heavy_workload_since is None


async def test_heavy_workload_clear_does_not_stomp_another_jobs_flag(monkeypatch):
    import genesis.runtime as runtime_mod

    # A concurrent job (e.g. dream_cycle) owns the flag; our clear must not touch it
    # if, mid-dispatch, the label no longer matches ours.
    fake_rt = types.SimpleNamespace(_heavy_workload=None, _heavy_workload_since=None)
    monkeypatch.setattr(
        runtime_mod.GenesisRuntime, "instance", staticmethod(lambda: fake_rt)
    )

    async def _steal(_t):
        # Simulate another job taking ownership during our execute.
        fake_rt._heavy_workload = "dream_cycle"
        return _Result(success=True)

    ctx = _make_ctx(executor=types.SimpleNamespace(execute=_steal))
    ctx._queue.next_task = AsyncMock(return_value=_Task(TaskType.J9_EVAL_BATCH))

    await dispatch.dispatch_once(ctx)

    # Label no longer ours → we must leave the other job's flag intact.
    assert fake_rt._heavy_workload == "dream_cycle"


async def test_heavy_workload_not_claimed_when_slot_already_held(monkeypatch):
    # A concurrent dream job already owns the single-slot flag BEFORE this dispatch
    # starts. We must NOT overwrite it (free-slot claim only) — otherwise our clear
    # would leave the dream unprotected mid-cycle. (RED against an unconditional set.)
    import genesis.runtime as runtime_mod

    fake_rt = types.SimpleNamespace(
        _heavy_workload="dream_cycle", _heavy_workload_since=datetime.now(UTC)
    )
    monkeypatch.setattr(
        runtime_mod.GenesisRuntime, "instance", staticmethod(lambda: fake_rt)
    )

    captured = {}

    async def _capture_execute(_t):
        captured["hw"] = fake_rt._heavy_workload
        return _Result(success=True)

    ctx = _make_ctx(executor=types.SimpleNamespace(execute=_capture_execute))
    ctx._queue.next_task = AsyncMock(return_value=_Task(TaskType.J9_EVAL_BATCH))

    await dispatch.dispatch_once(ctx)

    # Dream's flag untouched DURING our dispatch (we declined to claim)...
    assert captured["hw"] == "dream_cycle"
    # ...and NOT cleared afterward (we never owned it).
    assert fake_rt._heavy_workload == "dream_cycle"


async def test_heavy_workload_flag_set_during_setup_phase(monkeypatch):
    # 2026-09-01 incident: the surplus heartbeat can stall in a SETUP step
    # (recover_stuck / drain_expired / next_task) with NO task running. #1588 set the
    # flag only AFTER task selection, so a setup-phase stall was NOT forgiven by the
    # 900s zombie watchdog. The flag must now cover the WHOLE dispatch_once cycle —
    # set at entry, before recover_stuck — even on a no-task cycle.
    import genesis.runtime as runtime_mod

    fake_rt = types.SimpleNamespace(_heavy_workload=None, _heavy_workload_since=None)
    monkeypatch.setattr(
        runtime_mod.GenesisRuntime, "instance", staticmethod(lambda: fake_rt)
    )

    seen = {}

    async def _capture_during_setup():
        # The flag must ALREADY be set when the first setup phase runs.
        seen["hw"] = fake_rt._heavy_workload
        seen["since"] = fake_rt._heavy_workload_since

    ctx = _make_ctx()
    ctx._queue.recover_stuck = AsyncMock(side_effect=_capture_during_setup)
    ctx._queue.next_task = AsyncMock(return_value=None)  # no-task early return

    result = await dispatch.dispatch_once(ctx)

    assert result is False
    # Flag was set at ENTRY, before recover_stuck (RED on #1588 → None here).
    assert seen["hw"] == "surplus:dispatch"
    assert seen["since"] is not None
    # Cleared after the no-task early return — no leak.
    assert fake_rt._heavy_workload is None
    assert fake_rt._heavy_workload_since is None


# ── _timed_phase: diagnostic warning on an abnormally slow setup phase ──


def test_timed_phase_warns_when_slow(monkeypatch, caplog):
    # A setup phase exceeding the threshold logs a diagnostic WARNING (the surface
    # that would have localized the 2026-09-01 no-task-running stall).
    monkeypatch.setattr(dispatch, "_SLOW_PHASE_WARN_S", -1.0)  # any duration exceeds
    with caplog.at_level(logging.WARNING), dispatch._timed_phase("recover_stuck"):
        pass
    assert any(
        "recover_stuck" in r.getMessage() and "heartbeat-stall" in r.getMessage()
        for r in caplog.records
    )


def test_timed_phase_silent_when_fast(monkeypatch, caplog):
    monkeypatch.setattr(dispatch, "_SLOW_PHASE_WARN_S", 1e9)  # nothing exceeds
    with caplog.at_level(logging.WARNING), dispatch._timed_phase("recover_stuck"):
        pass
    assert not any("heartbeat-stall" in r.getMessage() for r in caplog.records)


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


# ── KB-routing gate (_route_insights) ───────────────────────────────────
# Non-KB-routing tasks (action/maintenance/monitor/pipeline-intermediate) must
# NOT ingest their operational-telemetry output into the knowledge base; only
# KB_ROUTING_TASK_TYPES (insight-producing + bookmark-enrichment) do.

import types as _types  # noqa: E402

from genesis.surplus import intake as _intake_mod  # noqa: E402
from genesis.surplus import quality_judge as _judge_mod  # noqa: E402

_LONG = "x" * 60  # clears the 50-char intake gate


def _intake_ctx():
    ctx = _make_ctx()
    ctx._judge_router = None
    return ctx


async def _run_route(task_type, monkeypatch):
    run_intake_spy = AsyncMock(
        return_value=_types.SimpleNamespace(
            findings_count=1, routed_knowledge=1, routed_observation=0,
            routed_discard=0, routed_staging=0, staged_ids=[],
        )
    )
    monkeypatch.setattr(_intake_mod, "run_intake", run_intake_spy)
    monkeypatch.setattr(
        _judge_mod, "run_quality_judge", AsyncMock(return_value=(None, None, None))
    )
    ctx = _intake_ctx()
    result = _Result(insights=[{"generating_model": "m", "confidence": 0.6}], content=_LONG)
    staging_id, *_ = await dispatch._route_insights(ctx, _Task(task_type), result)
    return run_intake_spy, staging_id


async def test_route_insights_skips_kb_intake_for_action_task(monkeypatch):
    # DB_MAINTENANCE is operational telemetry — must NOT reach run_intake.
    spy, staging_id = await _run_route(TaskType.DB_MAINTENANCE, monkeypatch)
    spy.assert_not_awaited()
    assert staging_id is not None  # still tracked


async def test_route_insights_skips_kb_intake_for_pipeline_intermediate(monkeypatch):
    spy, _ = await _run_route(TaskType.RESEARCH_QUERY_GEN, monkeypatch)
    spy.assert_not_awaited()


async def test_route_insights_ingests_insight_task(monkeypatch):
    # BRAINSTORM_SELF produces durable knowledge — must reach run_intake.
    spy, _ = await _run_route(TaskType.BRAINSTORM_SELF, monkeypatch)
    spy.assert_awaited_once()


async def test_route_insights_ingests_bookmark_enrichment(monkeypatch):
    # In KB_ROUTING (enriched bookmarks are durable) though NOT judge-eligible.
    spy, _ = await _run_route(TaskType.BOOKMARK_ENRICHMENT, monkeypatch)
    spy.assert_awaited_once()


# ── _filter_tiers_for_network — PR-3 outage awareness ──────────────────

_CLOUD_AND_LOCAL = [ComputeTier.FREE_API, ComputeTier.LOCAL_30B]
_CLOUD_ONLY = [ComputeTier.FREE_API]
_LOCAL_ONLY = [ComputeTier.LOCAL_30B]


def _patch_decision(monkeypatch, value):
    monkeypatch.setattr(network_config, "parking_decision", lambda now=None: value)


def test_filter_off_unchanged(monkeypatch):
    _patch_decision(monkeypatch, "off")
    assert dispatch._filter_tiers_for_network(list(_CLOUD_AND_LOCAL)) == _CLOUD_AND_LOCAL


def test_filter_normal_unchanged(monkeypatch):
    _patch_decision(monkeypatch, "normal")
    assert dispatch._filter_tiers_for_network(list(_CLOUD_AND_LOCAL)) == _CLOUD_AND_LOCAL


def test_filter_shadow_unchanged(monkeypatch):
    # Shadow observes only — tiers untouched (a "would filter" log fires).
    _patch_decision(monkeypatch, "shadow")
    assert dispatch._filter_tiers_for_network(list(_CLOUD_AND_LOCAL)) == _CLOUD_AND_LOCAL


def test_filter_park_strips_cloud_keeps_local(monkeypatch):
    _patch_decision(monkeypatch, "park")
    assert dispatch._filter_tiers_for_network(list(_CLOUD_AND_LOCAL)) == _LOCAL_ONLY


def test_filter_park_strips_all_when_no_local(monkeypatch):
    # Cloud-only during an outage → empty list (surplus goes quiet-pending).
    _patch_decision(monkeypatch, "park")
    assert dispatch._filter_tiers_for_network(list(_CLOUD_ONLY)) == []


def test_filter_park_noop_when_already_local(monkeypatch):
    _patch_decision(monkeypatch, "park")
    assert dispatch._filter_tiers_for_network(list(_LOCAL_ONLY)) == _LOCAL_ONLY


def test_filter_failsafe_on_error(monkeypatch):
    # Any error in the gate leaves tiers intact (never strips on uncertainty).
    def _boom(now=None):
        raise RuntimeError("gate broke")

    monkeypatch.setattr(network_config, "parking_decision", _boom)
    assert dispatch._filter_tiers_for_network(list(_CLOUD_AND_LOCAL)) == _CLOUD_AND_LOCAL


async def test_dispatch_once_offline_feeds_filtered_tiers_and_never_fails(monkeypatch):
    # OFFLINE+live+cloud-only → next_task receives [], no task runs, nothing is
    # marked failed (the task stays `pending` for the next cycle).
    _patch_decision(monkeypatch, "park")
    ctx = _make_ctx(idle=True)
    ctx._compute = _types.SimpleNamespace(
        get_available_tiers=AsyncMock(return_value=list(_CLOUD_ONLY)),
    )
    ctx._queue.next_task = AsyncMock(return_value=None)

    processed = await dispatch.dispatch_once(ctx)

    assert processed is False
    ctx._queue.next_task.assert_awaited_once_with([])  # cloud stripped → empty
    ctx._queue.mark_failed.assert_not_called()
    ctx._queue.mark_running.assert_not_called()
