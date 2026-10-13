"""Offline provider probe → breaker → snapshot contract checks."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from genesis.observability import provider_health as probes
from genesis.observability.health_data import HealthDataService
from genesis.observability.snapshots.call_sites import call_sites
from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.types import (
    CallSiteConfig,
    ErrorCategory,
    ProviderConfig,
    ProviderState,
    RoutingConfig,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for provider_type in ("NVIDIA_NIM", "GROQ", "GOOGLE", "ANTHROPIC", "UNKNOWN", "OLLAMA"):
        for name in (f"API_KEY_{provider_type}", f"{provider_type}_API_KEY", f"{provider_type}_API_TOKEN"):
            monkeypatch.delenv(name, raising=False)

    def forbid_network(*args, **kwargs):
        pytest.fail("an unmocked HTTP session was created")

    monkeypatch.setattr(probes.aiohttp, "ClientSession", forbid_network)
    monkeypatch.setitem(sys.modules, "genesis.runtime", SimpleNamespace(
        GenesisRuntime=SimpleNamespace(instance=lambda: SimpleNamespace()),
    ))


def provider(name="nvidia", provider_type="nvidia_nim", **kwargs):
    return ProviderConfig(
        name=name, provider_type=provider_type, model_id="synthetic-model",
        is_free=True, rpm_limit=20, open_duration_s=120, **kwargs,
    )


def config(*providers):
    return RoutingConfig(
        providers={p.name: p for p in providers},
        call_sites={"health_probe_test": CallSiteConfig(
            id="health_probe_test", chain=[p.name for p in providers], dispatch="api",
        )},
        retry_profiles={},
    )


@pytest.fixture
def http(monkeypatch):
    requests = []

    def install(status=200, payload=..., error=None):
        if payload is ...:
            payload = {"data": [{"id": "synthetic-model"}]}

        class Response:
            async def __aenter__(self):
                if error:
                    raise error
                return self

            async def __aexit__(self, *args):
                return False

            async def json(self):
                if isinstance(payload, Exception):
                    raise payload
                return payload

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, url, headers):
                requests.append((url, headers))
                return Response()

        Response.status = status
        monkeypatch.setattr(probes.aiohttp, "ClientSession", lambda **kwargs: Session())
        return requests

    return install


def test_imports_intended_worktree():
    expected = Path(__file__).resolve().parents[2] / "src/genesis/observability/provider_health.py"
    assert Path(probes.__file__).resolve() == expected


@pytest.mark.asyncio
async def test_configured_nvidia_is_probed(monkeypatch, http):
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "synthetic-key")
    requests = http()
    cfg = config(provider())
    breakers = CircuitBreakerRegistry(cfg.providers, persist=False)
    checker = probes.ProviderHealthChecker(cfg, breakers=breakers)
    results = await checker.probe_all()
    assert results["nvidia"].configured is True
    assert results["nvidia"].reachable is True
    assert requests == [("https://integrate.api.nvidia.com/v1/models", {
        "Authorization": "Bearer synthetic-key",
    })]
    snapshot = await call_sites(None, cfg, breakers, probe_results=results)
    site = snapshot["health_probe_test"]
    assert site["status"] == "healthy"
    assert site["chain_health"][0]["probe_status"] == "reachable"


@pytest.mark.asyncio
async def test_unsupported_probe_keeps_configured_provider(monkeypatch):
    monkeypatch.setenv("UNKNOWN_API_KEY", "synthetic-key")
    cfg = config(provider(provider_type="unknown"))
    breakers = CircuitBreakerRegistry(cfg.providers, persist=False)
    checker = probes.ProviderHealthChecker(cfg, breakers=breakers)
    results = await checker.probe_all()
    assert results["nvidia"].configured is True
    snapshot = await call_sites(None, cfg, breakers, probe_results=results)
    site = snapshot["health_probe_test"]
    assert site["status"] == "healthy"
    assert "probe_status" not in site["chain_health"][0]
    assert site["chain_health"][0]["probe_reason"] == "unsupported_probe"


@pytest.mark.parametrize("key_env", ["API_KEY_NVIDIA_NIM", "NVIDIA_NIM_API_KEY", "NVIDIA_NIM_API_TOKEN"])
@pytest.mark.asyncio
async def test_nvidia_key_conventions(monkeypatch, http, key_env):
    monkeypatch.setenv(key_env, "synthetic-key")
    requests = http()
    result = await probes.ProviderHealthChecker(config(provider()))._probe_one(provider())
    assert result.reachable and result.configured and result.probe_supported
    assert result.can_affect_breaker is False
    assert requests[0][1] == {"Authorization": "Bearer synthetic-key"}


@pytest.mark.parametrize("provider_type", ["nvidia_nim", "unknown"])
@pytest.mark.parametrize("key", [None, "", "NA", "None"])
@pytest.mark.asyncio
async def test_missing_credentials_are_not_probed(monkeypatch, tmp_path, provider_type, key):
    if key is not None:
        monkeypatch.setenv(f"{provider_type.upper()}_API_KEY", key)
    cfg = config(provider(provider_type=provider_type, has_api_key=False))
    breakers = CircuitBreakerRegistry(cfg.providers, persist=False)
    checker = probes.ProviderHealthChecker(cfg, breakers=breakers)
    results = await checker.probe_all()
    result = results["nvidia"]
    assert not result.configured and not result.reachable
    assert result.probe_supported == (provider_type == "nvidia_nim")
    assert result.error == "no API key configured"
    snap = await call_sites(None, cfg, breakers, probe_results=results)
    assert snap["health_probe_test"]["status"] == "disabled"
    assert snap["health_probe_test"]["chain_health"][0]["probe_reason"] == "no_api_key"
    serialized = HealthDataService(provider_health_checker=checker)._serialize_provider_health()
    assert serialized["nvidia"]["configured"] is False
    (tmp_path / "nvidia_health_e2e.json").write_text(json.dumps({
        "case": f"missing-key/{provider_type}/{key}",
        "provider_health": serialized, "call_sites": snap,
    }))


@pytest.mark.parametrize(("ptype", "base_url", "expected_url", "headers", "payload"), [
    ("groq", None, "https://api.groq.com/openai/v1/models",
     {"Authorization": "Bearer synthetic-key"}, {"data": [{"id": "synthetic-model"}]}),
    ("anthropic", None, "https://api.anthropic.com/v1/models",
     {"x-api-key": "synthetic-key", "anthropic-version": "2023-06-01"},
     {"data": [{"id": "synthetic-model"}]}),
    ("google", None, "https://generativelanguage.googleapis.com/v1beta/models?key=synthetic-key",
     {}, {"models": [{"name": "models/synthetic-model"}]}),
    ("google", "https://provider.invalid/v1/", "https://provider.invalid/v1/models",
     {}, {"models": [{"name": "models/synthetic-model"}]}),
    ("unknown", "https://provider.invalid/v1/", "https://provider.invalid/v1/models",
     {"Authorization": "Bearer synthetic-key"}, {"data": [{"id": "synthetic-model"}]}),
    ("ollama", "http://local.invalid/", "http://local.invalid/api/tags",
     {}, {"models": [{"name": "synthetic-model"}]}),
])
@pytest.mark.asyncio
async def test_existing_request_shapes(monkeypatch, http, ptype, base_url, expected_url, headers, payload):
    if ptype != "ollama":
        monkeypatch.setenv(f"{ptype.upper()}_API_KEY", "synthetic-key")
    requests = http(payload=payload)
    p = provider(provider_type=ptype, base_url=base_url)
    result = await probes.ProviderHealthChecker(config(p))._probe_one(p)
    assert result.reachable and result.model_available and result.probe_supported
    assert result.can_affect_breaker is True
    assert requests == [(expected_url, headers)]


@pytest.mark.asyncio
async def test_google_errors_do_not_expose_query_key(monkeypatch, http):
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-key")
    http(status=401)
    p = provider(provider_type="google")
    result = await probes.ProviderHealthChecker(config(p))._probe_one(p)
    assert result.error == "HTTP 401 from https://generativelanguage.googleapis.com/v1beta/models"
    assert "synthetic-key" not in result.error


@pytest.mark.parametrize("outcome", ["listed", "unlisted", "non_json", "wrong_shape", "401", "403", "429", "timeout"])
@pytest.mark.parametrize("hold", ["closed", "probe", "auth", "quota", "entitlement", "operator", "legacy"])
@pytest.mark.asyncio
async def test_nvidia_observations_never_mutate_breakers(monkeypatch, http, tmp_path, outcome, hold):
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "synthetic-key")
    cfg = config(provider())
    state_file = tmp_path / "synthetic-breakers.json"
    if hold == "legacy":
        state_file.write_text(json.dumps({"nvidia": {
            "state": "half_open", "trip_count": 2, "consecutive_failures": 0,
        }}))
    breakers = CircuitBreakerRegistry(cfg.providers, state_file=state_file, clock=lambda: 0.0, persist=False)
    cb = breakers.get("nvidia")
    if hold == "probe":
        cb.probe_suspect()
    elif hold in ("auth", "quota", "entitlement"):
        category = {"auth": ErrorCategory.PERMANENT, "quota": ErrorCategory.QUOTA_EXHAUSTED,
                    "entitlement": ErrorCategory.NOT_ENTITLED}[hold]
        for _ in range(3):
            cb.record_failure(category)
    elif hold == "operator":
        cb.force_open()
    if hold == "legacy":
        assert cb.state == ProviderState.HALF_OPEN and cb._opened_by_call is False
    before = (cb.state, cb.trip_count, cb.consecutive_failures, cb._consecutive_successes,
              cb._opened_by_call, cb._failure_cause, cb._incident_identity)
    suspect = Mock(wraps=cb.probe_suspect)
    heal = Mock(wraps=cb.record_probe_success)
    monkeypatch.setattr(cb, "probe_suspect", suspect)
    monkeypatch.setattr(cb, "record_probe_success", heal)
    if outcome.isdigit():
        http(status=int(outcome))
    elif outcome == "timeout":
        http(error=TimeoutError("synthetic-key"))
    elif outcome == "non_json":
        http(payload=ValueError("not json"))
    elif outcome == "wrong_shape":
        http(payload=[])
    elif outcome == "unlisted":
        http(payload={"data": [{"id": "different-model"}]})
    else:
        http()
    checker = probes.ProviderHealthChecker(cfg, breakers=breakers)
    for _ in range(4):  # More than the probe-healing threshold.
        results = await checker.probe_all()
        assert results["nvidia"].can_affect_breaker is False
    assert (cb.state, cb.trip_count, cb.consecutive_failures, cb._consecutive_successes,
            cb._opened_by_call, cb._failure_cause, cb._incident_identity) == before
    suspect.assert_not_called()
    heal.assert_not_called()
    assert "synthetic-key" not in (results["nvidia"].error or "")
    snapshot = await call_sites(None, cfg, breakers, probe_results=results)
    serialized = HealthDataService(provider_health_checker=checker)._serialize_provider_health()
    (tmp_path / "nvidia_health_e2e.json").write_text(json.dumps({
        "case": f"{hold}/{outcome}", "provider_health": serialized, "call_sites": snapshot,
    }))
    if hold == "legacy":
        assert json.loads(state_file.read_text())["nvidia"].get("opened_by_call") is None


@pytest.mark.parametrize("ptype", ["nvidia_nim", "unknown"])
@pytest.mark.asyncio
async def test_gather_exception_keeps_observations_neutral(monkeypatch, ptype):
    monkeypatch.setenv(f"{ptype.upper()}_API_KEY", "synthetic-key")
    cfg = config(provider(provider_type=ptype))
    breakers = CircuitBreakerRegistry(cfg.providers, persist=False)
    checker = probes.ProviderHealthChecker(cfg, breakers=breakers)
    monkeypatch.setattr(checker, "_probe_one", AsyncMock(side_effect=RuntimeError("synthetic failure")))
    results = await checker.probe_all()
    assert breakers.get("nvidia").state == ProviderState.CLOSED
    assert results["nvidia"].can_affect_breaker is False


@pytest.mark.parametrize("state", [ProviderState.CLOSED, ProviderState.HALF_OPEN, ProviderState.OPEN])
@pytest.mark.asyncio
async def test_unsupported_display_preserves_breaker_and_fallback(monkeypatch, http, tmp_path, state):
    monkeypatch.setenv("UNKNOWN_API_KEY", "synthetic-key")
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-key")
    requests = http()
    cfg = config(provider(provider_type="unknown"), provider("fallback", "groq"))
    breakers = CircuitBreakerRegistry(cfg.providers, clock=lambda: 0.0, persist=False)
    cb = breakers.get("nvidia")
    if state == ProviderState.HALF_OPEN:
        cb.probe_suspect()
    elif state == ProviderState.OPEN:
        cb.force_open()
    checker = probes.ProviderHealthChecker(cfg, breakers=breakers)
    results = await checker.probe_all()
    snap = await call_sites(None, cfg, breakers, probe_results=results)
    entry = snap["health_probe_test"]["chain_health"][0]
    assert entry["state"] == state and "probe_status" not in entry
    assert entry["probe_reason"] == "unsupported_probe"
    assert snap["health_probe_test"]["status"] == ("healthy" if state == ProviderState.CLOSED else "degraded")
    assert len(requests) == 1  # Only the supported fallback was probed.
    (tmp_path / "nvidia_health_e2e.json").write_text(json.dumps({
        "case": f"unsupported/{state.value}",
        "provider_health": HealthDataService(provider_health_checker=checker)._serialize_provider_health(),
        "call_sites": snap,
    }))


@pytest.mark.parametrize("ptype", ["nvidia_nim", "unknown", "groq"])
@pytest.mark.asyncio
async def test_serializer_reports_support_without_claiming_authentication(monkeypatch, http, ptype):
    monkeypatch.setenv(f"{ptype.upper()}_API_KEY", "synthetic-key")
    http()
    cfg = config(provider(provider_type=ptype))
    checker = probes.ProviderHealthChecker(cfg)
    await checker.probe_all()
    row = HealthDataService(provider_health_checker=checker)._serialize_provider_health()["nvidia"]
    assert row["configured"] is True  # Presence, not validity or entitlement.
    assert row["probe_supported"] == (ptype != "unknown")
    assert row["can_affect_breaker"] == (ptype == "groq")
    assert row["reachable"] == (ptype != "unknown")


@pytest.mark.asyncio
async def test_result_distribution_and_serializer_evidence(monkeypatch, http, tmp_path):
    from genesis.observability import snapshots

    # Keep the actual aggregation/probe/call-site/serialization path. Isolate
    # unrelated system, database, service and memory observations from this VM.
    for name in (
        "cc_sessions", "infrastructure", "queues", "surplus_status", "cost",
        "awareness", "outreach_stats", "mcp_status", "provider_activity",
        "memory_health", "eval_staleness", "services_async", "deploy_health", "reflex",
    ):
        monkeypatch.setattr(snapshots, name, AsyncMock(return_value={}))
    for name in ("conversation_activity", "proactive_memory_metrics"):
        monkeypatch.setattr(snapshots, name, Mock(return_value={}))

    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "synthetic-key")
    requests = http()
    cfg = config(provider(), ProviderConfig(
        name="second", provider_type="nvidia_nim", model_id="other-model",
        is_free=True, rpm_limit=20, open_duration_s=120,
    ))
    breakers = CircuitBreakerRegistry(cfg.providers, persist=False)
    checker = probes.ProviderHealthChecker(cfg, breakers=breakers)
    service = HealthDataService(
        provider_health_checker=checker, routing_config=cfg, circuit_breakers=breakers,
    )
    health_snapshot = await service.snapshot()
    results = checker.results
    assert len(requests) == 1
    assert results["nvidia"].model_available is True
    assert results["second"].model_available is False
    assert all(r.probe_supported and not r.can_affect_breaker for r in results.values())
    serialized = health_snapshot["provider_health"]
    snap = health_snapshot["call_sites"]
    for entry in serialized.values():
        assert entry["configured"] and entry["probe_supported"]
        assert entry["can_affect_breaker"] is False
        assert {"reachable", "model_available", "latency_ms", "error", "checked_at"} <= entry.keys()
    (tmp_path / "nvidia_health_e2e.json").write_text(json.dumps({"provider_health": serialized, "call_sites": snap}))


@pytest.mark.asyncio
async def test_replacement_discards_old_observation(monkeypatch, http):
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "synthetic-key")
    http()
    old = config(provider())
    new = config(provider("replacement"))
    old_breakers = CircuitBreakerRegistry(old.providers, persist=False)
    new_breakers = CircuitBreakerRegistry(new.providers, persist=False)
    current = [old, {"nvidia": old_breakers.get("nvidia")}]
    checker = probes.ProviderHealthChecker(
        old, breakers=old_breakers, routing_snapshot=lambda: tuple(current),
    )
    original_probe = checker._probe_one
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(p):
        entered.set()
        await release.wait()
        return await original_probe(p)

    monkeypatch.setattr(checker, "_probe_one", delayed)
    pending = asyncio.create_task(checker.probe_all())
    await entered.wait()
    current[:] = [new, {"replacement": new_breakers.get("replacement")}]
    release.set()
    assert await pending == {}
    assert checker.results == {} and checker.is_stale()
    assert new_breakers.get("replacement").state == ProviderState.CLOSED
    results = await checker.probe_all()
    assert set(results) == {"replacement"}
    assert results["replacement"].probe_supported and not results["replacement"].can_affect_breaker
