"""Routing reload contracts, exercised through Router and the production delegate.

All provider responses are local mocks; state files stay in pytest tmp_path.
"""
import asyncio
from dataclasses import replace
from pathlib import Path
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
    ProviderState,
    RetryPolicy,
)
from tests.test_routing.generation_helpers import config, make_router, response


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


def test_different_alias_budgets_remain_separate(tmp_path):
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
    assert ledger.status(new)["requests_used"] == 0
    assert not ledger.exhausted(new)


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
def test_named_counters_keep_valid_units_and_other_alias_rows(tmp_path, old_day):
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
    expected = 3
    ledger = DailyBudgetLedger(state_path=path, clock=lambda: now)
    assert ledger.status(cfg)["requests_used"] == expected
    assert ledger.status(cfg)["tokens_used"] == 8
    ledger.record(cfg, CallResult(success=True))
    saved = json.loads(path.read_text())["providers"]
    assert set(saved) == ({legacy, current} if old_day == "current" else {current})
    for _ in range(3):
        ledger = DailyBudgetLedger(state_path=path, clock=lambda: now)
        assert ledger.status(cfg)["requests_used"] == expected + 1


def test_late_alias_usage_stays_on_original_named_counter(tmp_path):
    from genesis.routing.config import _RENAMED_PROVIDERS
    from genesis.routing.daily_budget import DailyBudgetLedger
    from genesis.routing.types import CallResult

    legacy, current = next(iter(_RENAMED_PROVIDERS.items()))
    old = replace(config(name=legacy).providers[legacy], rpd_limit=10)
    new = replace(old, name=current)
    ledger = DailyBudgetLedger(state_path=tmp_path / "usage.json", persist=False)
    ledger.record(old, CallResult(success=True))
    ledger.record(old, CallResult(success=True))  # late completion under the old alias
    assert ledger.status(new)["requests_used"] == 0
    assert ledger.status(old)["requests_used"] == 2
    ledger.record(new, CallResult(success=True))
    assert ledger.status(old)["requests_used"] == 2
    assert ledger.status(new)["requests_used"] == 1


def test_explicit_both_aliases_keep_independent_counters(tmp_path):
    from genesis.routing.config import _RENAMED_PROVIDERS
    from genesis.routing.daily_budget import DailyBudgetLedger
    from genesis.routing.types import CallResult

    legacy, current = next(iter(_RENAMED_PROVIDERS.items()))
    old = replace(config(name=legacy).providers[legacy], rpd_limit=10)
    new = replace(old, name=current)
    ledger = DailyBudgetLedger(state_path=tmp_path / "usage.json", persist=False)
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


def test_daily_dashboard_reader_and_provider_writer_are_serialized(tmp_path, monkeypatch):
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
    real_today = ledger._today
    errors, observed = [], []

    def blocking_today():
        if threading.current_thread().name == "dashboard" and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return real_today()

    monkeypatch.setattr(ledger, "_today", blocking_today)

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
    assert observed == [2]
    assert ledger.status(cfg)["requests_used"] == 3


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




@pytest.mark.asyncio
async def test_captured_resilience_matches_health_during_reload(tmp_path, monkeypatch):
    import genesis.observability.snapshots as snapshots
    from genesis.observability.health_data import HealthDataService
    from genesis.resilience.state import ResilienceStateMachine
    from genesis.routing.types import DegradationLevel
    cfg = config()
    cfg.call_sites["3_micro_reflection"] = CallSiteConfig(id="3_micro_reflection", chain=["provider"])
    router = make_router(cfg, tmp_path)
    old = router.breakers.get("provider")
    old._failure_threshold = 1
    old.record_failure(ErrorCategory.TRANSIENT, retirement=True)
    machine = ResilienceStateMachine()
    machine.update_cloud = MagicMock()
    service = HealthDataService(circuit_breakers=router.breakers, routing_config=cfg,
        routing_snapshot=router.health_snapshot, resilience_state_machine=machine)
    entered, release = asyncio.Event(), asyncio.Event()
    async def suspended(*args, **kwargs):
        entered.set()
        await release.wait()
        return {}
    for name in ["cc_sessions", "infrastructure", "queues", "surplus_status", "cost", "awareness",
                 "outreach_stats", "mcp_status", "provider_activity", "memory_health", "eval_staleness",
                 "services_async", "deploy_health", "reflex"]:
        monkeypatch.setattr(snapshots, name, AsyncMock(return_value={}))
    monkeypatch.setattr(snapshots, "infrastructure", suspended)
    for name in ["conversation_activity", "proactive_memory_metrics"]:
        monkeypatch.setattr(snapshots, name, MagicMock(return_value={}))
    monkeypatch.setattr("genesis.observability.snapshots.api_keys.resolve_api_key", lambda _: "test-key")
    service._vcr_snapshot = AsyncMock(return_value={})
    pending = asyncio.create_task(service._compute_snapshot())
    await asyncio.wait_for(entered.wait(), 5)
    router.reload_config(config("replacement"))
    release.set()
    result = await pending
    assert result["api_keys"]["providers"]["provider"]["cb_state"] == "open"
    assert result["resilience"]["level"] == DegradationLevel.ESSENTIAL.value
    assert result["resilience"]["summary"] == "Providers down: provider"
    assert router.breakers.get("provider").state == ProviderState.CLOSED
    machine.update_cloud.assert_not_called()
