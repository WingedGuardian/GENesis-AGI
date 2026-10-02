"""Routing reload contracts, exercised through Router and the production delegate.

All provider responses are local mocks; state files stay in pytest tmp_path.
"""
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.routing import router as router_module
from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.litellm_delegate import LiteLLMDelegate
from genesis.routing.router import Router
from genesis.routing.types import (
    BudgetStatus,
    CallSiteConfig,
    ErrorCategory,
    ProviderConfig,
    ProviderState,
    RetryPolicy,
    RoutingConfig,
)


def config(model="old-model", name="provider"):
    provider = ProviderConfig(name=name, provider_type="openai", model_id=model,
                              is_free=True, rpm_limit=None, open_duration_s=60)
    return RoutingConfig(providers={name: provider},
                         call_sites={"test": CallSiteConfig(id="test", chain=[name])},
                         retry_profiles={"default": RetryPolicy(max_retries=0)})


def make_router(cfg, tmp_path):
    tracker = MagicMock(db=None)
    tracker.check_budget = AsyncMock(return_value=BudgetStatus.UNDER_LIMIT)
    return Router(config=cfg,
                  breakers=CircuitBreakerRegistry(cfg.providers,
                      state_file=tmp_path / "breakers.json", persist=False),
                  cost_tracker=tracker, degradation=MagicMock(should_skip=lambda _: False),
                  delegate=LiteLLMDelegate(cfg, profile_registry=MagicMock()))


def response():
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                           usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1))


@pytest.mark.asyncio
async def test_reload_calls_replacement_model_and_reports_same_identity(tmp_path, monkeypatch):
    # The worktree guard is part of the test's evidence, not an env assumption.
    assert Path(router_module.__file__).resolve().parents[3] == Path(__file__).resolve().parents[2]
    completion = AsyncMock(return_value=response())
    monkeypatch.setattr("genesis.routing.litellm_delegate.litellm.acompletion", completion)
    router = make_router(config(), tmp_path)
    router.reload_config(config("new-model"))
    result = await router.route_call("test", [])
    assert result.success
    assert completion.call_args.kwargs["model"] == result.model_id == "new-model"


@pytest.mark.asyncio
async def test_request_waiting_on_budget_keeps_old_generation(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    router = make_router(config(), tmp_path)
    completion = AsyncMock(return_value=response())
    monkeypatch.setattr("genesis.routing.litellm_delegate.litellm.acompletion", completion)

    async def budget():
        entered.set()
        await release.wait()
        return BudgetStatus.UNDER_LIMIT

    router.cost_tracker.check_budget = budget
    pending = asyncio.create_task(router.route_call("test", []))
    await entered.wait()
    router.reload_config(config("new-model", "replacement"))
    release.set()
    result = await pending
    assert result.success
    assert result.provider_used == "provider"
    assert completion.call_args.kwargs["model"] == result.model_id == "old-model"


@pytest.mark.asyncio
async def test_late_old_failure_cannot_open_replacement_breaker(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    router = make_router(config(), tmp_path)
    old = router.breakers.get("provider")
    old._failure_threshold = 1

    async def fail(**kwargs):
        entered.set()
        await release.wait()
        raise RuntimeError("old model failed")

    monkeypatch.setattr("genesis.routing.litellm_delegate.litellm.acompletion", fail)
    pending = asyncio.create_task(router.route_call("test", []))
    await entered.wait()
    router.reload_config(config("new-model"))
    new = router.breakers.get("provider")
    release.set()
    await pending
    assert old.state == ProviderState.OPEN
    assert new is not old
    assert new.state == ProviderState.CLOSED
    assert new.consecutive_failures == 0


@pytest.mark.parametrize("hold", ["auth", "quota", "operator", "ambiguous"])
def test_replacement_preserves_non_retirement_holds(tmp_path, hold):
    router = make_router(config(), tmp_path)
    old = router.breakers.get("provider")
    old._failure_threshold = 1
    if hold == "operator":
        old.force_open()
    else:
        category = {"auth": ErrorCategory.PERMANENT,
                    "quota": ErrorCategory.QUOTA_EXHAUSTED,
                    "ambiguous": ErrorCategory.TRANSIENT}[hold]
        old.record_failure(category)
    router.reload_config(config("new-model"))
    new = router.breakers.get("provider")
    assert new.state == ProviderState.OPEN
    assert new.last_failure_category == old.last_failure_category


def test_breaker_reload_does_not_mutate_old_configuration(tmp_path):
    old = config()
    router = make_router(old, tmp_path)
    router.reload_config(config("new-model"))
    assert old.providers["provider"].model_id == "old-model"


def test_alias_rename_preserves_daily_usage(tmp_path):
    from genesis.routing.config import _RENAMED_PROVIDERS
    from genesis.routing.daily_budget import DailyBudgetLedger
    from genesis.routing.types import CallResult

    legacy, current = next(iter(_RENAMED_PROVIDERS.items()))
    old = replace(config(name=legacy).providers[legacy], rpd_limit=1)
    new = replace(old, name=current)
    ledger = DailyBudgetLedger(state_path=tmp_path / "budget.json", persist=False)
    ledger.record(old, CallResult(success=True, content="ok"))
    assert ledger.exhausted(old)
    assert ledger.status(old)["requests_used"] == 1
    assert ledger.status(new)["requests_used"] == 1
    assert ledger.exhausted(new)


@pytest.mark.parametrize("mode", ["reload", "restart"])
@pytest.mark.parametrize("cause", ["retirement", "auth", "quota", "entitlement",
                                  "operator", "legacy", "auth_then_retirement",
                                  "quota_then_retirement", "operator_then_retirement"])
def test_replacement_hold_matrix(tmp_path, mode, cause):
    import json

    old_config = config()
    path = tmp_path / "matrix.json"
    registry = CircuitBreakerRegistry(old_config.providers, state_file=path, clock=lambda: 0)
    cb = registry.get("provider")
    cb._failure_threshold = 1
    if cause == "operator" or cause == "operator_then_retirement":
        cb.force_open()
    elif cause.startswith("auth"):
        cb.record_failure(ErrorCategory.PERMANENT)
    elif cause.startswith("quota"):
        cb.record_failure(ErrorCategory.QUOTA_EXHAUSTED)
    elif cause == "entitlement":
        cb.record_failure(ErrorCategory.NOT_ENTITLED)
    else:
        cb.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    if cause.endswith("then_retirement"):
        cb.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    if cause == "legacy":
        cb._failure_cause = None
        cb._failure_identity = None
    registry.save_state()
    saved = json.loads(path.read_text())
    assert "old-model" not in path.read_text()  # only an identity digest is persisted
    assert saved["provider"]["identity"] is not None
    replacement = config("new-model")
    if mode == "reload":
        registry.update_providers(replacement.providers)
    else:
        registry = CircuitBreakerRegistry(replacement.providers, state_file=path, clock=lambda: 0)
    current = registry.get("provider")
    assert current.state == (ProviderState.CLOSED if cause == "retirement" else ProviderState.OPEN)


def test_retirement_does_not_clear_same_identity_or_alias_rename(tmp_path):
    from genesis.routing.config import _RENAMED_PROVIDERS

    legacy, current = next(iter(_RENAMED_PROVIDERS.items()))
    cfg = config(name=legacy)
    recovery = MagicMock()
    registry = CircuitBreakerRegistry(cfg.providers, state_file=tmp_path / "rename.json",
                                      clock=lambda: 0, on_recovery=recovery)
    old = registry.get(legacy)
    old._failure_threshold = 1
    old.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    registry.update_providers(config(name=current).providers)
    rebound = registry.get(current)
    assert rebound.state == ProviderState.OPEN
    rebound._state = ProviderState.HALF_OPEN
    rebound.record_success()
    rebound.record_success()
    recovery.assert_called_once_with(current)
    registry.save_state()
    saved = (tmp_path / "rename.json").read_text()
    # The old object's completion can neither resolve an observation nor overwrite state.
    old._state = ProviderState.HALF_OPEN
    old.record_success()
    old.record_success()
    assert recovery.call_count == 1
    assert (tmp_path / "rename.json").read_text() == saved


@pytest.mark.parametrize("wait_at", ["rate", "retry", "completion"])
@pytest.mark.asyncio
async def test_waiting_request_retains_model_and_rate_bindings(tmp_path, monkeypatch, wait_at):
    from genesis.routing.types import CallResult

    entered, release = asyncio.Event(), asyncio.Event()
    old_config = replace(config(), retry_profiles={"default": RetryPolicy(
        max_retries=1, base_delay_ms=1, jitter_pct=0)})
    router = make_router(old_config, tmp_path)
    old_gate = router._rate_gates
    called_models = []

    async def waiting(*args):
        entered.set()
        await release.wait()

    async def completion(**kwargs):
        called_models.append(kwargs["model"])
        if wait_at == "completion":
            await waiting()
        return response()

    monkeypatch.setattr("genesis.routing.litellm_delegate.litellm.acompletion", completion)
    if wait_at == "rate":
        monkeypatch.setattr(old_gate, "acquire", waiting)
    elif wait_at == "retry":
        original = router.delegate.call
        count = 0

        async def fail_then_call(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                return CallResult(success=False, status_code=500, error="transient")
            return await original(*args, **kwargs)

        monkeypatch.setattr(router.delegate, "call", fail_then_call)
        monkeypatch.setattr("genesis.routing.router.asyncio.sleep", waiting)
    pending = asyncio.create_task(router.route_call("test", []))
    await entered.wait()
    router.reload_config(config("new-model"))
    release.set()
    result = await pending
    assert result.model_id == "old-model"
    assert called_models == ["old-model"]
    assert router._rate_gates is not old_gate


@pytest.mark.asyncio
async def test_failed_reload_preparation_keeps_previous_generation(tmp_path, monkeypatch):
    router = make_router(config(), tmp_path)
    bindings = router.config, router.delegate, router._rate_gates, router.breakers.get("provider")

    def fail(*_):
        raise ValueError("preparation failed")

    monkeypatch.setattr(router._rate_gates, "reconfigured", fail)
    with pytest.raises(ValueError, match="preparation failed"):
        router.reload_config(config("new-model"))
    assert bindings == (router.config, router.delegate, router._rate_gates,
                        router.breakers.get("provider"))
    completion = AsyncMock(return_value=response())
    monkeypatch.setattr("genesis.routing.litellm_delegate.litellm.acompletion", completion)
    result = await router.route_call("test", [])
    assert result.success and result.model_id == "old-model"


@pytest.mark.parametrize("status,error,reached,expected", [
    (410, "model retired", True, True), (410, "account gone", True, False),
    (404, "model retired", True, False), (401, "model retired", True, False),
    (410, "model retired", False, False), (410, "", True, False),
])
def test_retirement_requires_specific_evidence(status, error, reached, expected):
    from genesis.routing.provider_identity import retirement_failure
    from genesis.routing.types import CallResult

    assert retirement_failure(CallResult(success=False, status_code=status, error=error,
                                         reached_provider=reached)) is expected


@pytest.mark.parametrize("old_day", ["current", "stale"])
def test_alias_counters_merge_once_and_keep_valid_units(tmp_path, old_day):
    import json
    from datetime import UTC, datetime

    from genesis.routing.config import _RENAMED_PROVIDERS
    from genesis.routing.daily_budget import DailyBudgetLedger
    from genesis.routing.types import CallResult

    legacy, current = next(iter(_RENAMED_PROVIDERS.items()))
    now = datetime(2026, 10, 1, tzinfo=UTC)
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"version": 1, "providers": {
        legacy: {"day": "2026-10-01" if old_day == "current" else "2026-09-30",
                 "requests": 2, "tokens": -1},
        current: {"day": "2026-10-01", "requests": 3, "tokens": 8},
    }}))
    cfg = replace(config(name=current).providers[current], rpd_limit=100)
    expected = 5 if old_day == "current" else 3
    ledger = DailyBudgetLedger(state_path=path, clock=lambda: now)
    assert ledger.status(cfg)["requests_used"] == expected
    assert ledger.status(cfg)["tokens_used"] == 8
    ledger.record(cfg, CallResult(success=True))
    saved = json.loads(path.read_text())["providers"]
    assert list(saved) == [current]
    for _ in range(3):
        ledger = DailyBudgetLedger(state_path=path, clock=lambda: now)
        assert ledger.status(cfg)["requests_used"] == expected + 1


def test_late_alias_usage_and_rollback_share_current_counter(tmp_path):
    from genesis.routing.config import _RENAMED_PROVIDERS
    from genesis.routing.daily_budget import DailyBudgetLedger
    from genesis.routing.types import CallResult

    legacy, current = next(iter(_RENAMED_PROVIDERS.items()))
    old = replace(config(name=legacy).providers[legacy], rpd_limit=10)
    new = replace(old, name=current)
    ledger = DailyBudgetLedger(state_path=tmp_path / "usage.json", persist=False)
    ledger.bind_providers({legacy: old})
    ledger.record(old, CallResult(success=True))
    ledger.bind_providers({current: new})
    ledger.record(old, CallResult(success=True))  # late completion under the old alias
    assert ledger.status(new)["requests_used"] == 2
    ledger.bind_providers({legacy: old})
    assert ledger.status(old)["requests_used"] == 2
    ledger.record(new, CallResult(success=True))
    assert ledger.status(old)["requests_used"] == 3


def test_explicit_both_aliases_keep_independent_counters(tmp_path):
    from genesis.routing.config import _RENAMED_PROVIDERS
    from genesis.routing.daily_budget import DailyBudgetLedger
    from genesis.routing.types import CallResult

    legacy, current = next(iter(_RENAMED_PROVIDERS.items()))
    old = replace(config(name=legacy).providers[legacy], rpd_limit=10)
    new = replace(old, name=current)
    ledger = DailyBudgetLedger(state_path=tmp_path / "usage.json", persist=False)
    ledger.bind_providers({legacy: old, current: new})
    ledger.record(old, CallResult(success=True))
    assert ledger.status(old)["requests_used"] == 1
    assert ledger.status(new)["requests_used"] == 0


@pytest.mark.asyncio
async def test_router_reload_restart_records_actual_request_identity_e2e(tmp_path, monkeypatch):
    """Real delegate, parser, cost recorder, daily ledger and persisted restart.

    Only the provider HTTP completion and its quoted cost are mocked.
    """
    import aiosqlite

    from genesis.db.schema import create_all_tables
    from genesis.routing.cost_tracker import CostTracker
    from genesis.routing.daily_budget import DailyBudgetLedger

    cfg = config()
    cfg = replace(cfg, providers={"provider": replace(cfg.providers["provider"],
                                                    is_free=False, rpd_limit=10)})
    new_cfg = replace(cfg, providers={"provider": replace(cfg.providers["provider"],
                                                        model_id="new-model")})
    sent = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def complete(**kwargs):
        sent.append(kwargs["model"])
        if kwargs["model"] == "old-model":
            entered.set()
            await release.wait()
        return response()

    monkeypatch.setattr("genesis.routing.litellm_delegate.litellm.acompletion", complete)
    monkeypatch.setattr("genesis.routing.litellm_delegate.litellm.completion_cost", lambda **_: .01)
    state_path, daily_path = tmp_path / "breakers.json", tmp_path / "daily.json"
    async with aiosqlite.connect(tmp_path / "e2e.sqlite") as db:
        db.row_factory = aiosqlite.Row
        await create_all_tables(db)
        tracker = CostTracker(db)
        daily = DailyBudgetLedger(state_path=daily_path)
        router = Router(cfg, CircuitBreakerRegistry(cfg.providers, state_file=state_path),
                        tracker, MagicMock(should_skip=lambda _: False), LiteLLMDelegate(cfg),
                        daily_budget=daily)
        pending = asyncio.create_task(router.route_call("test", [], budget_override=True))
        await entered.wait()
        router.reload_config(new_cfg)
        release.set()
        assert (await pending).model_id == "old-model"
        result = await router.route_call("test", [], budget_override=True)
        assert result.success and result.model_id == "new-model"
        row = await (await db.execute("SELECT model_id FROM call_site_last_run WHERE call_site_id='test'")).fetchone()
        assert row["model_id"] == sent[-1] == "new-model"
        assert await tracker.get_period_cost("today") == pytest.approx(.02)
        assert daily.status(new_cfg.providers["provider"])["requests_used"] == 2
        restarted = Router(new_cfg, CircuitBreakerRegistry(new_cfg.providers, state_file=state_path),
                           tracker, MagicMock(should_skip=lambda _: False), LiteLLMDelegate(new_cfg),
                           daily_budget=DailyBudgetLedger(state_path=daily_path))
        result = await restarted.route_call("test", [], budget_override=True)
        assert result.success and result.model_id == "new-model"
        assert restarted._daily_budget.status(new_cfg.providers["provider"])["requests_used"] == 3
        assert sent == ["old-model", "new-model", "new-model"]


@pytest.mark.asyncio
async def test_mixed_retries_do_not_become_retirement_only(tmp_path):
    from genesis.routing.types import CallResult

    cfg = replace(config(), retry_profiles={"default": RetryPolicy(max_retries=1, base_delay_ms=0)})
    router = make_router(cfg, tmp_path)
    router.delegate.call = AsyncMock(side_effect=[
        CallResult(success=False, status_code=500, error="unknown outage"),
        CallResult(success=False, status_code=410, error="model retired"),
    ])
    router.breakers.get("provider")._failure_threshold = 1
    assert not (await router.route_call("test", [])).success
    assert router.breakers.get("provider")._failure_cause == "other"
    router.reload_config(config("new-model"))
    assert router.breakers.get("provider").state == ProviderState.OPEN


@pytest.mark.parametrize("message", ["model is not retired", "model not retired",
    "account retired: model access removed", "model retired because account quota exhausted",
    "model decommissioned?", "model newer-model retired"])
def test_ambiguous_retirement_messages_preserve_hold(message):
    from genesis.routing.provider_identity import retirement_failure
    from genesis.routing.types import CallResult

    assert not retirement_failure(CallResult(success=False, status_code=410, error=message), "old-model")


@pytest.mark.parametrize("field,value", [("identity", "f" * 64),
    ("last_failure_category", "quota_exhausted"), ("opened_by_call", False),
    ("failure_identity", "unknown")])
def test_inconsistent_persisted_provenance_never_clears_hold(tmp_path, field, value):
    import json

    cfg = config()
    path = tmp_path / "invalid-provenance.json"
    registry = CircuitBreakerRegistry(cfg.providers, state_file=path, clock=lambda: 0)
    cb = registry.get("provider")
    cb._failure_threshold = 1
    cb.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    saved = json.loads(path.read_text())
    saved["provider"][field] = value
    path.write_text(json.dumps(saved))
    restarted = CircuitBreakerRegistry(config("new-model").providers,
                                        state_file=path, clock=lambda: 0)
    assert restarted.get("provider").state == ProviderState.OPEN


@pytest.mark.parametrize("mode", ["reload", "restart"])
def test_operator_hold_survives_alias_rollback(tmp_path, mode):
    from genesis.routing.config import _RENAMED_PROVIDERS

    legacy, current = next(iter(_RENAMED_PROVIDERS.items()))
    cfg = config(name=current)
    path = tmp_path / "rollback.json"
    registry = CircuitBreakerRegistry(cfg.providers, state_file=path, clock=lambda: 0)
    registry.get(current).force_open()
    if mode == "reload":
        registry.update_providers(config(name=legacy).providers)
    else:
        registry = CircuitBreakerRegistry(config(name=legacy).providers, state_file=path, clock=lambda: 0)
    assert registry.get(legacy).state == ProviderState.OPEN
    assert registry.get(legacy)._failure_cause == "operator"


def test_daily_dashboard_reader_and_alias_writer_are_serialized(tmp_path, monkeypatch):
    import threading

    from genesis.routing.config import _RENAMED_PROVIDERS
    from genesis.routing.daily_budget import DailyBudgetLedger
    from genesis.routing.types import CallResult

    legacy, current = next(iter(_RENAMED_PROVIDERS.items()))
    cfg = replace(config(name=current).providers[current], rpd_limit=100)
    ledger = DailyBudgetLedger(state_path=tmp_path / "concurrent.json", persist=False)
    today = ledger._today()
    ledger._counters = {legacy: {"day": today, "requests": 1, "tokens": 0},
                        current: {"day": today, "requests": 2, "tokens": 0}}
    entered, release, writer_started = threading.Event(), threading.Event(), threading.Event()
    real_name = ledger._name
    errors, observed = [], []

    def blocking_name(name):
        if threading.current_thread().name == "dashboard" and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return real_name(name)

    monkeypatch.setattr(ledger, "_name", blocking_name)

    def reader():
        try:
            observed.append(ledger.status(cfg)["requests_used"])
        except Exception as exc:
            errors.append(exc)

    def writer():
        writer_started.set()
        ledger.record(cfg, CallResult(success=True))

    read_thread = threading.Thread(target=reader, name="dashboard")
    write_thread = threading.Thread(target=writer, name="server")
    read_thread.start()
    assert entered.wait(5)
    write_thread.start()
    assert writer_started.wait(5)
    release.set()
    read_thread.join(5)
    write_thread.join(5)
    assert not read_thread.is_alive() and not write_thread.is_alive()
    assert not errors
    assert observed == [3]
    assert ledger.status(cfg)["requests_used"] == 4


@pytest.mark.asyncio
async def test_health_probe_uses_current_generation_and_discards_late_probe(tmp_path):
    from genesis.observability.provider_health import ProviderHealthChecker, ProviderProbeResult

    router = make_router(config(), tmp_path)
    checker = ProviderHealthChecker(router.config, breakers=router.breakers,
                                    routing_snapshot=router.health_snapshot)
    entered, release = asyncio.Event(), asyncio.Event()
    probed = []

    async def probe(cfg):
        probed.append(cfg.model_id)
        if cfg.model_id == "old-model":
            entered.set()
            await release.wait()
            return ProviderProbeResult(provider_name=cfg.name, reachable=False, error="old endpoint down")
        return ProviderProbeResult(provider_name=cfg.name, reachable=True,
                                   model_available=True, _models=frozenset({cfg.model_id}))

    checker._probe_one = probe
    pending = asyncio.create_task(checker.probe_all())
    await entered.wait()
    router.reload_config(config("new-model"))
    release.set()
    assert await pending == {}
    assert router.breakers.get("provider").state == ProviderState.CLOSED
    assert checker.results == {} and checker.is_stale()
    result = await checker.probe_all()
    assert result["provider"].model_available
    assert probed == ["old-model", "new-model"]


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["operator", "auth", "quota"])
@pytest.mark.parametrize("essential", [False, True])
async def test_health_snapshot_and_key_validation_read_replacement_config(tmp_path, monkeypatch, hold, essential):
    from genesis.observability.health_data import HealthDataService
    from genesis.observability.snapshots.call_sites import call_sites as actual_call_sites

    cfg = config()
    if essential:
        cfg.call_sites["3_micro_reflection"] = CallSiteConfig(id="3_micro_reflection", chain=["provider"])
    router = make_router(cfg, tmp_path)
    service = HealthDataService(circuit_breakers=router.breakers, routing_config=router.config,
                                routing_snapshot=router.health_snapshot)
    # Isolate unrelated health subsystems while retaining the actual routing renderer.
    import genesis.observability.snapshots as snapshots
    seen = []

    async def render(db, cfg, breakers, **kwargs):
        seen.append(cfg)
        return await actual_call_sites(None, cfg, breakers, **kwargs)

    monkeypatch.setattr(snapshots, "call_sites", render)
    async_names = ["cc_sessions", "infrastructure", "queues", "surplus_status", "cost",
                   "awareness", "outreach_stats", "mcp_status", "provider_activity", "memory_health",
                   "eval_staleness", "services_async", "deploy_health", "reflex"]
    for name in async_names:
        monkeypatch.setattr(snapshots, name, AsyncMock(return_value={}))
    for name in ["conversation_activity", "proactive_memory_metrics"]:
        monkeypatch.setattr(snapshots, name, MagicMock(return_value={}))
    service._vcr_snapshot = AsyncMock(return_value={})
    service._resilience_state = MagicMock(return_value={})
    monkeypatch.setattr("genesis.observability.snapshots.api_keys.resolve_api_key", lambda _: "local-test-key")
    cb = router.breakers.get("provider")
    if hold == "operator":
        cb.force_open()
    else:
        category = ErrorCategory.PERMANENT if hold == "auth" else ErrorCategory.QUOTA_EXHAUSTED
        for _ in range(3):
            cb.record_failure(category)
    old = await service.snapshot(max_age_s=300)
    key = old["api_keys"]["providers"]["provider"]
    assert key["cb_state"] == "open"
    assert key["key_health"] == "red"
    assert key["alert_severity"] == ("critical" if essential else "info")
    assert old["call_sites"]["test"]["chain_health"][0]["model"] == "old-model"
    replacement = config("new-model")
    router.reload_config(replacement)
    new = await service.snapshot(max_age_s=300)
    assert new["call_sites"]["test"]["chain_health"][0]["model"] == "new-model"
    validate = AsyncMock()
    monkeypatch.setattr("genesis.observability.snapshots.api_keys.validate_api_keys", validate)
    await service.validate_api_keys()
    validate.assert_awaited_once_with(replacement)
    assert len(seen) == 2
    assert seen[0].providers["provider"].model_id == "old-model"
    assert seen[1] is replacement


@pytest.mark.asyncio
async def test_actual_litellm_retirement_error_is_recognized(tmp_path, monkeypatch):
    import litellm

    from genesis.routing.provider_identity import retirement_failure

    router = make_router(config(), tmp_path)
    error = litellm.APIError(status_code=410, message='model retired', llm_provider='openai', model='old-model')
    monkeypatch.setattr('genesis.routing.litellm_delegate.litellm.acompletion', AsyncMock(side_effect=error))
    result = await router.delegate.call('provider', 'old-model', [])
    assert result.status_code == 410
    assert retirement_failure(result, 'old-model')


@pytest.mark.parametrize('message', [
    'model is not retired', 'account retired', 'model retired but account disabled',
    'model retired: credentials invalid', 'litellm.APIError: model retired',
])
def test_sdk_prefix_does_not_widen_retirement_evidence(message):
    from genesis.routing.provider_identity import retirement_failure
    from genesis.routing.types import CallResult

    assert not retirement_failure(CallResult(success=False, status_code=410, error='litellm.APIError: '+message))


def test_toggle_targets_replacement_and_persists(tmp_path):
    import json

    path = tmp_path/'toggle.json'
    old_config = config()
    registry = CircuitBreakerRegistry(old_config.providers, state_file=path)
    captured = registry.get('provider')
    registry.update_providers(config('new-model').providers)
    assert registry.toggle('provider') is ProviderState.OPEN
    current = registry.get('provider')
    assert current is not captured
    assert current._failure_cause == 'operator'
    assert json.loads(path.read_text())['provider']['state'] == 'open'
    assert registry.toggle('provider') is ProviderState.CLOSED
    with pytest.raises(KeyError):
        registry.toggle('removed')


def test_toggle_and_reload_share_atomic_registry_lock(tmp_path):
    import threading

    registry = CircuitBreakerRegistry(config().providers, state_file=tmp_path/'state.json')
    entered, release = threading.Event(), threading.Event()
    errors = []

    def reload():
        entered.set()
        try:
            registry.update_providers(config('new-model').providers)
        except Exception as error:
            errors.append(error)
        finally:
            release.set()

    with registry._lock:
        worker = threading.Thread(target=reload)
        worker.start()
        assert entered.wait(2)
        assert not release.wait(.05)
        assert registry.toggle('provider') is ProviderState.OPEN
    worker.join(2)
    assert not worker.is_alive() and not errors
    assert registry.get('provider').state is ProviderState.OPEN
    assert registry.get('provider')._failure_cause == 'operator'


@pytest.mark.asyncio
@pytest.mark.parametrize('pause_bus', [False, True])
async def test_retired_trip_cannot_escalate_current_model(tmp_path, pause_bus):
    from genesis.observability.events import GenesisEventBus
    from genesis.routing.escalation import ProviderEscalation
    from genesis.routing.types import CallResult

    router = make_router(config(), tmp_path)
    event_bus = GenesisEventBus()
    router._event_bus = event_bus
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting_listener(event):
        if event.event_type == 'breaker.tripped':
            entered.set()
            await release.wait()

    if pause_bus:
        event_bus.subscribe(waiting_listener)
    escalation = ProviderEscalation(None, event_bus, current_identity=router.breakers.current_identity)
    escalation.attach()
    cb = router.breakers.get('provider')
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)

    class FailedDelegate:
        async def call(self, *args, **kwargs):
            if not pause_bus:
                entered.set()
                await release.wait()
            return CallResult(success=False, status_code=410, error='model retired')

    router.delegate = FailedDelegate()
    task = asyncio.create_task(router.route_call('test', []))
    await entered.wait()
    router.reload_config(config('new-model'))
    release.set()
    await task
    assert 'provider' not in escalation._state


@pytest.mark.parametrize('rename', [False, True])
def test_reload_retains_pacing_admission(tmp_path, rename):
    old_name, new_name = ('glm51', 'glm') if rename else ('provider', 'provider')
    old = config(name=old_name)
    old = replace(old, providers={old_name:replace(old.providers[old_name],rpm_limit=60)})
    router = make_router(old, tmp_path)
    gate = router._rate_gates._gates[old_name]
    gate._last_request = 123.0
    new = config('new-model',name=new_name)
    new = replace(new, providers={new_name:replace(new.providers[new_name],rpm_limit=30)})
    router.reload_config(new)
    assert router._rate_gates._gates[new_name] is gate
    assert gate._last_request == 123.0
    assert gate.interval == 2.0


async def drain_escalation_tasks():
    tasks = [task for task in asyncio.all_tasks()
             if task is not asyncio.current_task() and task.get_name().startswith("escalation-")]
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("already_escalated", [False, True])
async def test_retirement_replacement_starts_fresh_durable_incident(tmp_path, empty_db, already_escalated):
    from genesis.observability.events import GenesisEventBus
    from genesis.observability.types import Severity, Subsystem
    from genesis.routing.escalation import ProviderEscalation, sweep_due_notifications
    from genesis.routing.provider_identity import provider_identity

    bus = GenesisEventBus()
    escalation = ProviderEscalation(empty_db, bus,
        current_identity=lambda name: registry.current_identity(name),
        current_incident_identity=lambda name: registry.current_incident_identity(name))
    registry = CircuitBreakerRegistry(config().providers, state_file=tmp_path / "incidents.json",
        on_retirement=escalation.record_retirement)
    escalation.attach()

    async def trip():
        await bus.emit(Subsystem.ROUTING, Severity.WARNING, "breaker.tripped", "test",
                       provider="provider", health_identity=provider_identity(registry.get("provider")._provider),
                       incident_identity=registry.current_incident_identity("provider"))

    for _ in range(5 if already_escalated else 4):
        await trip()
    await drain_escalation_tasks()
    old = registry.get("provider")
    old._failure_threshold = 1
    old.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    registry.update_providers(config("new-model").providers)
    incident = registry.current_incident_identity("provider")
    assert incident is not None
    await drain_escalation_tasks()
    await trip()
    assert escalation._state["provider"]["trip_count"] == 1
    assert not escalation._state["provider"]["escalated"]
    assert escalation._state["provider"]["incident_identity"] == incident
    cursor = await empty_db.execute("SELECT resolved, resolution_notes FROM observations WHERE content_hash = ?",
                                   (escalation._provider_content_hash("provider"),))
    rows = await cursor.fetchall()
    if already_escalated:
        assert rows and all(row[0] == 1 and "retired" in row[1] and "recovered" not in row[1] for row in rows)
    else:
        assert not rows
    assert await sweep_due_notifications(empty_db, current_incident_identity=registry.current_incident_identity,
                                         provider_still_failing=lambda _: True) == 0
    for _ in range(4):
        await trip()
    await drain_escalation_tasks()
    cursor = await empty_db.execute("SELECT resolved FROM observations WHERE content_hash = ?",
                                   (escalation._provider_content_hash("provider", incident),))
    assert [row[0] for row in await cursor.fetchall()] == [0]
    restored = CircuitBreakerRegistry(config("new-model").providers, state_file=tmp_path / "incidents.json")
    assert restored.current_incident_identity("provider") == incident
    # A -> B -> A does not revive historical A hashes.
    new = registry.get("provider")
    new._failure_threshold = 1
    new.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    registry.update_providers(config().providers)
    assert registry.current_incident_identity("provider") not in (None, incident)
    await drain_escalation_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["operator", "auth", "quota", "ambiguous"])
async def test_repoint_preserves_account_escalation_history(tmp_path, empty_db, hold):
    from genesis.observability.events import GenesisEventBus
    from genesis.observability.types import Severity, Subsystem
    from genesis.routing.escalation import ProviderEscalation

    bus = GenesisEventBus()
    escalation = ProviderEscalation(empty_db, bus,
        current_identity=lambda name: registry.current_identity(name),
        current_incident_identity=lambda name: registry.current_incident_identity(name))
    registry = CircuitBreakerRegistry(config().providers, state_file=tmp_path / "account.json",
        on_retirement=escalation.record_retirement)
    escalation.attach()
    for _ in range(4):
        await bus.emit(Subsystem.ROUTING, Severity.WARNING, "breaker.tripped", "test", provider="provider")
    old = registry.get("provider")
    old._failure_threshold = 1
    if hold == "operator":
        old.force_open()
    else:
        old.record_failure({"auth": ErrorCategory.PERMANENT, "quota": ErrorCategory.QUOTA_EXHAUSTED,
                            "ambiguous": ErrorCategory.TRANSIENT}[hold])
    registry.update_providers(config("new-model").providers)
    assert registry.current_incident_identity("provider") is None
    await bus.emit(Subsystem.ROUTING, Severity.WARNING, "breaker.tripped", "test", provider="provider")
    await drain_escalation_tasks()
    assert escalation._state["provider"]["trip_count"] == 5
    assert escalation._state["provider"]["escalated"]


def test_restart_retires_model_with_fresh_persisted_incident(tmp_path):
    path = tmp_path / "restart-incident.json"
    registry = CircuitBreakerRegistry(config().providers, state_file=path)
    old = registry.get("provider")
    old._failure_threshold = 1
    old.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    retired = MagicMock()
    new = CircuitBreakerRegistry(config("new-model").providers, state_file=path, on_retirement=retired)
    incident = new.current_incident_identity("provider")
    assert incident is not None
    retired.assert_called_once_with("provider", None)
    assert new.get("provider").state == ProviderState.CLOSED
    again = CircuitBreakerRegistry(config("new-model").providers, state_file=path)
    assert again.current_incident_identity("provider") == incident


@pytest.mark.asyncio
async def test_retired_deferred_observation_is_not_left_actionable(empty_db, monkeypatch):
    from genesis.db.crud import observations
    from genesis.observability.events import GenesisEventBus
    from genesis.routing.escalation import ProviderEscalation

    current = [None]
    escalation = ProviderEscalation(empty_db, GenesisEventBus(), current_incident_identity=lambda _: current[0])
    state = {"incident_identity": None, "trip_count": 5, "first_trip_at": "2026-10-01T00:00:00+00:00",
             "last_trip_at": "2026-10-01T00:01:00+00:00", "escalated": False}
    entered, release = asyncio.Event(), asyncio.Event()
    actual_create = observations.create

    async def suspended_create(*args, **kwargs):
        entered.set()
        await release.wait()
        return await actual_create(*args, **kwargs)

    monkeypatch.setattr(observations, "create", suspended_create)
    pending = asyncio.create_task(escalation._create_observation("provider", state))
    await entered.wait()
    current[0] = "a" * 64
    escalation.record_retirement("provider", None)
    await drain_escalation_tasks()
    release.set()
    await pending
    cursor = await empty_db.execute("SELECT resolved, resolution_notes FROM observations WHERE content_hash = ?",
                                   (escalation._provider_content_hash("provider"),))
    rows = await cursor.fetchall()
    assert rows and all(row[0] == 1 and "retired" in row[1] for row in rows)
    assert not state["escalated"]


@pytest.mark.asyncio
async def test_retired_deferred_notification_is_not_left_actionable(empty_db, monkeypatch):
    import json
    from datetime import UTC, datetime, timedelta

    from genesis.db.crud import observations
    from genesis.routing.escalation import ProviderEscalation, notify_provider_if_due

    now = datetime.now(UTC)
    incident = "a" * 64
    current = [incident]
    await observations.create(empty_db, id="old-failure", source="routing", type="provider_failure",
        content=json.dumps({"provider": "provider", "incident_identity": incident}), priority="high",
        created_at=(now - timedelta(hours=2)).isoformat(),
        content_hash=ProviderEscalation._provider_content_hash("provider", incident))
    entered, release = asyncio.Event(), asyncio.Event()
    actual_create = observations.create

    async def suspended_create(*args, **kwargs):
        entered.set()
        await release.wait()
        return await actual_create(*args, **kwargs)

    monkeypatch.setattr(observations, "create", suspended_create)
    pending = asyncio.create_task(notify_provider_if_due(empty_db, "provider", clock=lambda: now,
        incident_identity=incident, current_incident_identity=lambda _: current[0],
        provider_still_failing=lambda _: True))
    await entered.wait()
    current[0] = "b" * 64
    release.set()
    assert not await pending
    cursor = await empty_db.execute("SELECT resolved, resolution_notes FROM observations WHERE content_hash = ?",
                                   (ProviderEscalation._notify_content_hash("provider", incident),))
    rows = await cursor.fetchall()
    assert rows and all(row[0] == 1 and "retired" in row[1] for row in rows)


def test_health_view_never_restores_or_writes_disk_and_keeps_captured_coverage(tmp_path, monkeypatch):
    cfg = config()
    cfg.call_sites["3_micro_reflection"] = CallSiteConfig(id="3_micro_reflection", chain=["provider"])
    router = make_router(cfg, tmp_path)
    old = router.breakers.get("provider")
    old._failure_threshold = 1
    old.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    captured_cfg, bindings = router.health_snapshot()
    router.reload_config(config("new-model"))
    monkeypatch.setattr(CircuitBreakerRegistry, "load_state", MagicMock(side_effect=AssertionError("disk read")))
    view = CircuitBreakerRegistry.health_view(captured_cfg, bindings)
    assert view.get("provider") is old
    assert view.uncovered_essential_sites() == ["3_micro_reflection"]
    assert router.breakers.get("provider").state == ProviderState.CLOSED
    assert view._persist is False
    view.save_state()


@pytest.mark.asyncio
async def test_awareness_notification_uses_current_retirement_incident(tmp_path, empty_db, monkeypatch):
    import json
    from datetime import UTC, datetime, timedelta

    from genesis.awareness.loop import _check_provider_outage_notify
    from genesis.db.crud import observations
    from genesis.routing.escalation import ProviderEscalation
    from genesis.runtime import GenesisRuntime

    router = make_router(config(), tmp_path)
    old = router.breakers.get("provider")
    old._failure_threshold = 1
    old.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    router.reload_config(config("new-model"))
    router.breakers.get("provider").force_open()
    incident = router.breakers.current_incident_identity("provider")
    assert incident is not None
    await observations.create(empty_db, id="current-failure", source="routing", type="provider_failure",
        content=json.dumps({"provider": "provider", "incident_identity": incident}), priority="high",
        created_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        content_hash=ProviderEscalation._provider_content_hash("provider", incident))
    monkeypatch.setattr(GenesisRuntime, "instance", lambda: SimpleNamespace(_circuit_breakers=router.breakers))
    monkeypatch.setattr("genesis.awareness.provider_notify_config.effective_mode", lambda: "live")
    await _check_provider_outage_notify(empty_db)
    await _check_provider_outage_notify(empty_db)
    cursor = await empty_db.execute("SELECT content_hash, content FROM observations WHERE priority='critical' AND resolved=0")
    rows = await cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == ProviderEscalation._notify_content_hash("provider", incident)
    assert json.loads(rows[0][1])["incident_identity"] == incident


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["observation", "notification"])
@pytest.mark.parametrize("replacement", ["rename", "remove"])
@pytest.mark.parametrize("legacy_incident", [True, False])
async def test_missing_alias_retires_deferred_database_write(
        tmp_path, empty_db, monkeypatch, writer, replacement, legacy_incident):
    import json
    from datetime import UTC, datetime, timedelta

    from genesis.db.crud import observations
    from genesis.observability.events import GenesisEventBus
    from genesis.routing.escalation import ProviderEscalation, notify_provider_if_due

    now = datetime.now(UTC)
    escalation = ProviderEscalation(empty_db, GenesisEventBus(),
        current_incident_identity=lambda name: registry.current_incident_identity(name))
    registry = CircuitBreakerRegistry(config("predecessor", "glm51").providers,
        state_file=tmp_path / "missing-alias.json", on_retirement=escalation.record_retirement)
    if not legacy_incident:
        predecessor = registry.get("glm51")
        predecessor._failure_threshold = 1
        predecessor.record_failure(ErrorCategory.TRANSIENT, retirement=True)
        registry.update_providers(config("old-model", "glm51").providers)
        await drain_escalation_tasks()
    incident = registry.current_incident_identity("glm51")
    assert (incident is None) == legacy_incident
    old = registry.get("glm51")
    old._failure_threshold = 1
    old.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    state = {"incident_identity": incident, "trip_count": 5, "first_trip_at": now.isoformat(),
             "last_trip_at": now.isoformat(), "escalated": False}
    if writer == "notification":
        await observations.create(empty_db, id="old-outage", source="routing", type="provider_failure",
            content=json.dumps({"provider": "glm51", "incident_identity": incident}), priority="high",
            created_at=(now - timedelta(hours=2)).isoformat(),
            content_hash=escalation._provider_content_hash("glm51", incident))
    entered, release = asyncio.Event(), asyncio.Event()
    actual_create = observations.create

    async def suspended_create(*args, **kwargs):
        entered.set()
        await release.wait()
        return await actual_create(*args, **kwargs)

    monkeypatch.setattr(observations, "create", suspended_create)
    if writer == "observation":
        pending = asyncio.create_task(escalation._create_observation("glm51", state))
        content_hash = escalation._provider_content_hash("glm51", incident)
    else:
        pending = asyncio.create_task(notify_provider_if_due(empty_db, "glm51", clock=lambda: now,
            incident_identity=incident, current_incident_identity=registry.current_incident_identity,
            provider_still_failing=lambda name: registry.get(name).state != ProviderState.CLOSED))
        content_hash = escalation._notify_content_hash("glm51", incident)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        registry.update_providers(config("new-model", "glm").providers if replacement == "rename" else {})
        await drain_escalation_tasks()
        if replacement == "rename":
            assert registry.get("glm").state == ProviderState.CLOSED
            assert registry.current_incident_identity("glm") not in (None, incident)
    finally:
        release.set()
    result = await pending
    if writer == "notification":
        assert result is False
    cursor = await empty_db.execute("SELECT resolved, resolution_notes FROM observations WHERE content_hash = ?",
                                   (content_hash,))
    rows = await cursor.fetchall()
    assert rows and all(row[0] == 1 and "retired" in row[1] and "recovered" not in row[1] for row in rows)
    assert not state["escalated"]
    # Absence is stale before a write too; live None must remain a valid incident.
    assert not escalation._incident_is_current("glm51", incident)
    assert not escalation._incident_is_current("glm51", None)
    if replacement == "rename":
        assert escalation._incident_is_current("glm", registry.current_incident_identity("glm"))
