"""Tests for real signal collector implementations (ErrorSpike, CriticalFailure)."""

from genesis.awareness.signals import CriticalFailureCollector, ErrorSpikeCollector
from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.types import ErrorCategory, ProviderConfig, ProviderState


def _make_providers(*specs: tuple[str, str]) -> dict[str, ProviderConfig]:
    """Create provider configs from (name, provider_type) tuples."""
    return {
        name: ProviderConfig(
            name=name,
            provider_type=ptype,
            model_id=f"{name}-model",
            is_free=False,
            rpm_limit=60,
            open_duration_s=120,
        )
        for name, ptype in specs
    }


def _trip_breaker(registry: CircuitBreakerRegistry, name: str) -> None:
    """Trip a breaker by recording enough transient failures."""
    cb = registry.get(name)
    for _ in range(3):
        cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.state == ProviderState.OPEN


# ── ErrorSpikeCollector ──────────────────────────────────────────────────────


async def test_error_spike_no_registry():
    """No registry → 0.0."""
    c = ErrorSpikeCollector()
    r = await c.collect()
    assert r.value == 0.0
    assert r.name == "software_error_spike"


async def test_error_spike_one_of_three_open():
    """1 of 3 breakers open → ~0.33."""
    providers = _make_providers(("a", "openai"), ("b", "openai"), ("c", "ollama"))
    reg = CircuitBreakerRegistry(providers)
    # Touch all breakers so they exist
    for name in providers:
        reg.get(name)
    _trip_breaker(reg, "a")

    c = ErrorSpikeCollector(registry=reg)
    r = await c.collect()
    assert abs(r.value - 1 / 3) < 0.01


async def test_error_spike_all_open():
    """All breakers open → 1.0."""
    providers = _make_providers(("a", "openai"), ("b", "anthropic"))
    reg = CircuitBreakerRegistry(providers)
    for name in providers:
        _trip_breaker(reg, name)

    c = ErrorSpikeCollector(registry=reg)
    r = await c.collect()
    assert r.value == 1.0


async def test_error_spike_none_open():
    """No breakers open → 0.0."""
    providers = _make_providers(("a", "openai"), ("b", "anthropic"))
    reg = CircuitBreakerRegistry(providers)
    for name in providers:
        reg.get(name)

    c = ErrorSpikeCollector(registry=reg)
    r = await c.collect()
    assert r.value == 0.0


# ── CriticalFailureCollector ─────────────────────────────────────────────────


async def test_critical_failure_no_registry():
    """No registry → 0.0."""
    c = CriticalFailureCollector()
    r = await c.collect()
    assert r.value == 0.0
    assert r.name == "critical_failure"


async def test_critical_failure_all_cloud_open():
    """All cloud breakers open → 1.0."""
    providers = _make_providers(("a", "openai"), ("b", "anthropic"), ("c", "ollama"))
    reg = CircuitBreakerRegistry(providers)
    for name in providers:
        reg.get(name)
    _trip_breaker(reg, "a")
    _trip_breaker(reg, "b")

    c = CriticalFailureCollector(registry=reg)
    r = await c.collect()
    assert r.value == 1.0


async def test_critical_failure_some_cloud_open():
    """Some but not all cloud breakers open → 0.0."""
    providers = _make_providers(("a", "openai"), ("b", "anthropic"), ("c", "ollama"))
    reg = CircuitBreakerRegistry(providers)
    for name in providers:
        reg.get(name)
    _trip_breaker(reg, "a")
    # b is still closed

    c = CriticalFailureCollector(registry=reg)
    r = await c.collect()
    assert r.value == 0.0


async def test_critical_failure_no_cloud_providers():
    """Only ollama providers → stub 0.0."""
    providers = _make_providers(("a", "ollama"), ("b", "ollama"))
    reg = CircuitBreakerRegistry(providers)
    for name in providers:
        reg.get(name)

    c = CriticalFailureCollector(registry=reg)
    r = await c.collect()
    assert r.value == 0.0
