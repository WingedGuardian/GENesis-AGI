"""ProviderHealthChecker._sync_to_breakers heals a HALF_OPEN breaker on a clean
probe (the low/no-traffic recovery path) and still only downgrades on failure.
"""

from __future__ import annotations

from genesis.observability.provider_health import (
    ProviderHealthChecker,
    ProviderProbeResult,
)
from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.types import (
    ErrorCategory,
    ProviderConfig,
    ProviderState,
    RoutingConfig,
)


def _provider(name: str = "free-1") -> ProviderConfig:
    return ProviderConfig(
        name=name, provider_type="groq", model_id="m",
        is_free=True, rpm_limit=None, open_duration_s=10,
    )


def _config(provider: ProviderConfig) -> RoutingConfig:
    return RoutingConfig(
        providers={provider.name: provider}, call_sites={}, retry_profiles={},
    )


def _half_open_registry():
    """Registry with free-1 driven to HALF_OPEN."""
    t = [0.0]
    prov = _provider("free-1")
    reg = CircuitBreakerRegistry({"free-1": prov}, clock=lambda: t[0], persist=False)
    cb = reg.get("free-1")
    for _ in range(3):  # default failure_threshold = 3
        cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.state == ProviderState.OPEN
    t[0] = 100.0  # past the open window
    assert cb.state == ProviderState.HALF_OPEN
    return reg, prov


def test_clean_probe_heals_half_open_breaker():
    """A reachable + model_available probe on a HALF_OPEN provider advances it to
    CLOSED via record_probe_success after the default probe threshold (3)."""
    reg, prov = _half_open_registry()
    checker = ProviderHealthChecker(_config(prov), breakers=reg)
    checker._results = {
        "free-1": ProviderProbeResult(
            provider_name="free-1", reachable=True, configured=True,
            model_available=True,
        ),
    }
    # default probe_success_threshold = 3 → three clean syncs heal it.
    for _ in range(3):
        checker._sync_to_breakers()
    assert reg.get("free-1").state == ProviderState.CLOSED


def test_probe_model_unavailable_does_not_heal():
    """Reachable but model_available is False/None (endpoint up but this model
    not listed) must NOT heal — weaker signal than a real completion."""
    reg, prov = _half_open_registry()
    checker = ProviderHealthChecker(_config(prov), breakers=reg)
    checker._results = {
        "free-1": ProviderProbeResult(
            provider_name="free-1", reachable=True, configured=True,
            model_available=None,
        ),
    }
    for _ in range(5):
        checker._sync_to_breakers()
    assert reg.get("free-1").state == ProviderState.HALF_OPEN


def test_unreachable_probe_still_only_downgrades():
    """Regression: an unreachable probe must still only downgrade a CLOSED
    breaker to HALF_OPEN (probe_suspect), never heal."""
    prov = _provider("free-1")
    reg = CircuitBreakerRegistry({"free-1": prov}, clock=lambda: 0, persist=False)
    assert reg.get("free-1").state == ProviderState.CLOSED
    checker = ProviderHealthChecker(_config(prov), breakers=reg)
    checker._results = {
        "free-1": ProviderProbeResult(
            provider_name="free-1", reachable=False, configured=True,
            error="ConnectionError",
        ),
    }
    checker._sync_to_breakers()
    assert reg.get("free-1").state == ProviderState.HALF_OPEN
