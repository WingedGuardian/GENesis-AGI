"""Captured pacing policy and current health projection across routing reloads."""
import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.observability.health_data import HealthDataService
from genesis.resilience.state import CloudStatus, ResilienceStateMachine
from genesis.resilience.status_writer import StatusFileWriter
from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.rate_gate import RateGateRegistry
from genesis.routing.types import CallSiteConfig, DegradationLevel
from tests.test_routing.generation_helpers import config, make_router, response


def paced_config(rpm, name="provider"):
    cfg = config(name=name)
    return replace(cfg, providers={name: replace(cfg.providers[name], rpm_limit=rpm)})


@pytest.mark.asyncio
@pytest.mark.parametrize("rename", [False, True])
@pytest.mark.parametrize("old_rpm,new_rpm", [(1, 60), (60, 1), (60, 60)])
@pytest.mark.parametrize("queued", [False, True])
async def test_captured_policy_shares_admissions_without_mutation(
    monkeypatch, rename, old_rpm, new_rpm, queued,
):
    old_name, new_name = ("glm51", "glm") if rename else ("provider", "provider")
    old = RateGateRegistry()
    old.register(old_name, old_rpm)
    old_gate = old._gates[old_name]
    original_sleep = asyncio.sleep
    clock = [100.0]
    old_gate._last_request = clock[0]
    monkeypatch.setattr("genesis.routing.rate_gate.time.monotonic", lambda: clock[0])

    async def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr("genesis.routing.rate_gate.asyncio.sleep", sleep)
    pending = None
    if queued:
        await old_gate._lock.acquire()
        pending = asyncio.create_task(old.acquire(old_name))
        await original_sleep(0)
        assert not pending.done()
    new = old.reconfigured(paced_config(new_rpm, new_name).providers,
                           lambda *_: new_name if rename else None)
    new_gate = new._gates[new_name]
    assert old_gate.interval == 60 / old_rpm
    assert new_gate.interval == 60 / new_rpm
    assert old_gate._lock is new_gate._lock
    if queued:
        old_gate._lock.release()
    waited = await pending if pending else await old.acquire(old_name)
    assert waited == 60 / old_rpm
    assert new_gate._last_request == clock[0]
    assert not await new_gate.try_acquire()
    assert await new.acquire(new_name) == 60 / new_rpm
    assert old_gate._last_request == new_gate._last_request == clock[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("rename", [False, True])
@pytest.mark.parametrize("old_rpm,new_rpm", [(1, 60), (60, 1)])
async def test_router_paused_before_admission_keeps_captured_rpm(
    tmp_path, monkeypatch, rename, old_rpm, new_rpm,
):
    old_name, new_name = ("glm51", "glm") if rename else ("provider", "provider")
    router = make_router(paced_config(old_rpm, old_name), tmp_path)
    clock = [100.0]
    router._rate_gates._gates[old_name]._last_request = clock[0]
    monkeypatch.setattr("genesis.routing.rate_gate.time.monotonic", lambda: clock[0])
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("genesis.routing.rate_gate.asyncio.sleep", sleep)
    entered, release = asyncio.Event(), asyncio.Event()

    async def budget():
        from genesis.routing.types import BudgetStatus
        entered.set()
        await release.wait()
        return BudgetStatus.UNDER_LIMIT

    router.cost_tracker.check_budget = budget
    monkeypatch.setattr("genesis.routing.litellm_delegate.litellm.acompletion",
                        AsyncMock(return_value=response()))
    pending = asyncio.create_task(router.route_call("test", []))
    await entered.wait()
    router.reload_config(paced_config(new_rpm, new_name))
    release.set()
    assert (await pending).success
    assert waits == [60 / old_rpm]
    assert router._rate_gates._gates[new_name]._last_request == clock[0]


def essential_config():
    cfg = config()
    sites = {site: CallSiteConfig(id=site, chain=["provider"])
                      for site in ("9_fact_extraction", "3_micro_reflection",
                                   "4_light_reflection", "40_ego_focus_selection")}
    return replace(cfg, call_sites=sites)


def isolate_sections(monkeypatch):
    import genesis.observability.snapshots as snapshots
    for name in ("cc_sessions", "infrastructure", "queues", "surplus_status", "cost",
                 "awareness", "outreach_stats", "mcp_status", "provider_activity",
                 "memory_health", "eval_staleness", "services_async", "deploy_health", "reflex"):
        monkeypatch.setattr(snapshots, name, AsyncMock(return_value={}))
    for name in ("conversation_activity", "proactive_memory_metrics"):
        monkeypatch.setattr(snapshots, name, MagicMock(return_value={}))
    monkeypatch.setattr("genesis.observability.snapshots.api_keys.resolve_api_key", lambda _: "test-key")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "readonly", "standalone"])
async def test_health_failure_and_recovery_reach_live_status(tmp_path, monkeypatch, mode):
    isolate_sections(monkeypatch)
    cfg = essential_config()
    router = make_router(cfg, tmp_path)
    router.breakers = CircuitBreakerRegistry(
        cfg.providers, persist=False, restore_state=False,
        essential_sites={site: ["provider"] for site in cfg.call_sites},
    )
    machine = ResilienceStateMachine()
    if mode == "standalone":
        service = HealthDataService(circuit_breakers=router.breakers, routing_config=cfg,
                                    resilience_state_machine=machine)
    else:
        from genesis.runtime.init.health_data import init
        rt = SimpleNamespace(_router=router, _circuit_breakers=router.breakers,
                             _resilience_state_machine=machine, _full=mode == "full")
        for name in ("_cost_tracker", "_cc_budget_tracker", "_deferred_work_queue",
                     "_dead_letter_queue", "_db", "_surplus_scheduler", "_learning_scheduler",
                     "_activity_tracker", "_event_bus", "_job_retry_registry"):
            setattr(rt, name, None)
        monkeypatch.setattr("genesis.mcp.health_mcp.init_health_mcp", lambda *a, **kw: None)
        monkeypatch.setattr("genesis.observability.provider_health.ProviderHealthChecker", MagicMock(return_value=None))
        init(rt)
        service = rt._health_data
    writer = StatusFileWriter(state_machine=machine, runtime=SimpleNamespace(),
                              path=str(tmp_path / "status.json"))
    for opened, level, cloud in [(True, DegradationLevel.ESSENTIAL, CloudStatus.ESSENTIAL),
                                  (False, DegradationLevel.NORMAL, CloudStatus.NORMAL)]:
        breaker = router.breakers.get("provider")
        breaker.force_open() if opened else breaker.force_close()
        result = await service._compute_snapshot()
        assert result["resilience"]["level"] == level.value
        assert machine.current.cloud == cloud
        await writer.write()
        assert json.loads((tmp_path / "status.json").read_text())["resilience_state"]["cloud"] == cloud.name


def test_projection_and_reload_are_atomic(tmp_path):
    cfg = essential_config()
    router = make_router(cfg, tmp_path)
    captured_cfg, bindings = router.health_snapshot()
    view = CircuitBreakerRegistry.health_view(captured_cfg, bindings)
    machine = ResilienceStateMachine()
    original = machine.update_cloud
    entered, finished = threading.Event(), threading.Event()

    def reload():
        entered.set()
        router.reload_config(essential_config())
        finished.set()

    worker = threading.Thread(target=reload)

    def update(cloud):
        worker.start()
        assert entered.wait(5)
        assert not finished.wait(0.05)
        original(cloud)

    machine.update_cloud = update
    router.health_resilience(captured_cfg, view, machine)
    worker.join(5)
    assert finished.is_set()
    machine.update_cloud = MagicMock()
    router.health_resilience(captured_cfg, view, machine)
    machine.update_cloud.assert_not_called()


@pytest.mark.parametrize("rpm", [None, 0, -1])
def test_disabling_limit_does_not_mutate_captured_policy(rpm):
    old = RateGateRegistry()
    old.register("provider", 1)
    gate = old._gates["provider"]
    gate._last_request = 123.0
    new = old.reconfigured(paced_config(rpm).providers, lambda *_: None)
    assert not new.has_gate("provider")
    assert gate.interval == 60.0
    assert gate._last_request == 123.0


def test_enabling_limit_creates_independent_admissions():
    old = RateGateRegistry()
    new = old.reconfigured(paced_config(60).providers, lambda *_: None)
    assert not old.has_gate("provider")
    assert new._gates["provider"].interval == 1.0
    assert new._gates["provider"]._last_request == 0.0
