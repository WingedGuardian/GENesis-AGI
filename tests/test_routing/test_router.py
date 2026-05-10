"""Tests for the Router."""

import pytest

from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.cost_tracker import CostTracker
from genesis.routing.degradation import DegradationTracker
from genesis.routing.router import Router
from genesis.routing.types import (
    CallResult,
    DegradationLevel,
    ErrorCategory,
)

from .conftest import MockDelegate


@pytest.fixture
def delegate():
    return MockDelegate()


@pytest.fixture
def breakers(sample_providers):
    return CircuitBreakerRegistry(sample_providers)


@pytest.fixture
def degradation():
    return DegradationTracker()


@pytest.fixture
async def cost_tracker(db):
    return CostTracker(db)


@pytest.fixture
async def router(sample_config, breakers, cost_tracker, degradation, delegate):
    return Router(
        config=sample_config,
        breakers=breakers,
        cost_tracker=cost_tracker,
        degradation=degradation,
        delegate=delegate,
    )


@pytest.mark.asyncio
async def test_first_provider_succeeds(router, delegate):
    result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])
    assert result.success is True
    assert result.provider_used == "free-1"
    assert result.content == "mock response"
    assert result.fallback_used is False
    assert len(delegate.calls) == 1


@pytest.mark.asyncio
async def test_fallback_on_failure(sample_config, breakers, cost_tracker, degradation):
    delegate = MockDelegate(responses={
        "free-1": CallResult(success=False, error="service unavailable", status_code=503),
    })
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])
    assert result.success is True
    assert result.provider_used == "free-2"
    assert result.fallback_used is True
    assert "free-1" in result.failed_providers


@pytest.mark.asyncio
async def test_never_pays_skips_paid(sample_config, breakers, cost_tracker, degradation):
    delegate = MockDelegate(responses={
        "free-1": CallResult(success=False, error="down", status_code=503),
        "free-2": CallResult(success=False, error="down", status_code=503),
    })
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("test_never_pays", [{"role": "user", "content": "hi"}])
    assert result.success is False
    # Only free providers should have been called
    providers_called = {c["provider"] for c in delegate.calls}
    assert "paid-1" not in providers_called
    assert "paid-2" not in providers_called


@pytest.mark.asyncio
async def test_budget_exceeded_skips_paid(sample_config, breakers, cost_tracker, degradation):
    """When budget is exceeded, paid providers are skipped (free-1 used instead)."""
    # Spend over the daily $2 budget
    for _ in range(210):
        await cost_tracker.record("test", "paid-1", CallResult(success=True, cost_usd=0.01))

    delegate = MockDelegate()
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])
    assert result.success is True
    # Should have used a free provider since paid is budget-blocked
    assert result.provider_used in ("free-1", "free-2")


@pytest.mark.asyncio
async def test_budget_override_allows_paid(sample_config, breakers, cost_tracker, degradation):
    """budget_override=True bypasses budget checks for paid providers."""
    for _ in range(210):
        await cost_tracker.record("test", "paid-1", CallResult(success=True, cost_usd=0.01))

    delegate = MockDelegate(responses={
        "free-1": CallResult(success=False, error="down", status_code=503),
        "free-2": CallResult(success=False, error="down", status_code=503),
    })
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call(
        "test_mixed", [{"role": "user", "content": "hi"}], budget_override=True,
    )
    assert result.success is True
    assert result.provider_used == "paid-1"


@pytest.mark.asyncio
async def test_skips_open_breaker(sample_config, breakers, cost_tracker, degradation):
    """A tripped circuit breaker causes the router to skip that provider."""
    # Trip free-1's breaker
    cb = breakers.get("free-1")
    for _ in range(3):
        cb.record_failure(ErrorCategory.TRANSIENT)

    delegate = MockDelegate()
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])
    assert result.success is True
    assert result.provider_used == "free-2"
    # free-1 should not have been called
    assert all(c["provider"] != "free-1" for c in delegate.calls)


@pytest.mark.asyncio
async def test_skips_keyless_provider(
    sample_providers, breakers, cost_tracker, degradation,
):
    """A provider with has_api_key=False must be skipped without ever
    calling the delegate — same effect as a tripped breaker, but driven
    by config rather than runtime state. No CB trip, no failure record.
    """
    import dataclasses

    from genesis.routing.types import CallSiteConfig, RetryPolicy, RoutingConfig

    # Mark free-1 as keyless
    providers = dict(sample_providers)
    providers["free-1"] = dataclasses.replace(providers["free-1"], has_api_key=False)

    config = RoutingConfig(
        providers=providers,
        call_sites={
            "test_keyless": CallSiteConfig(id="test_keyless", chain=["free-1", "free-2"]),
        },
        retry_profiles={"default": RetryPolicy(max_retries=1, base_delay_ms=10, jitter_pct=0.0)},
    )

    delegate = MockDelegate()
    router = Router(
        config=config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("test_keyless", [{"role": "user", "content": "hi"}])

    # Successful routing — free-2 handled it
    assert result.success is True
    assert result.provider_used == "free-2"
    # free-1 must never have been called
    assert all(c["provider"] != "free-1" for c in delegate.calls)
    # free-1's CB must NOT have tripped — we never tried, never failed
    assert breakers.get("free-1").consecutive_failures == 0
    assert breakers.get("free-1").trip_count == 0


@pytest.mark.asyncio
async def test_all_keyless_chain_returns_exhausted(
    sample_providers, breakers, cost_tracker, degradation,
):
    """If every provider in the chain is keyless, routing fails with the
    standard exhausted-chain error — no LiteLLM calls, no CB trips.
    """
    import dataclasses

    from genesis.routing.types import CallSiteConfig, RetryPolicy, RoutingConfig

    providers = {
        name: dataclasses.replace(cfg, has_api_key=False)
        for name, cfg in sample_providers.items()
    }
    config = RoutingConfig(
        providers=providers,
        call_sites={
            "all_keyless": CallSiteConfig(id="all_keyless", chain=["free-1", "free-2"]),
        },
        retry_profiles={"default": RetryPolicy(max_retries=1, base_delay_ms=10, jitter_pct=0.0)},
    )
    delegate = MockDelegate()
    router = Router(
        config=config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("all_keyless", [{"role": "user", "content": "hi"}])

    assert result.success is False
    # No CB trips on any provider
    for name in providers:
        assert breakers.get(name).trip_count == 0
    # Delegate was never called
    assert len(delegate.calls) == 0


@pytest.mark.asyncio
async def test_degradation_skips_call_site(sample_config, breakers, cost_tracker, degradation):
    """At L2 degradation, surplus call sites are skipped."""
    degradation.update(DegradationLevel.REDUCED)

    delegate = MockDelegate()
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    # We need a call site that L2 skips. Add it to config.
    from genesis.routing.types import CallSiteConfig
    sample_config.call_sites["12_surplus_brainstorm"] = CallSiteConfig(
        id="12_surplus_brainstorm", chain=["free-1"],
    )
    result = await router.route_call("12_surplus_brainstorm", [{"role": "user", "content": "hi"}])
    assert result.success is False
    assert "degradation" in (result.error or "").lower()
    assert len(delegate.calls) == 0


@pytest.mark.asyncio
async def test_all_providers_exhausted(sample_config, breakers, cost_tracker, degradation):
    delegate = MockDelegate(responses={
        "paid-1": CallResult(success=False, error="down", status_code=503),
        "paid-2": CallResult(success=False, error="down", status_code=503),
    })
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("test_paid", [{"role": "user", "content": "hi"}])
    assert result.success is False
    assert result.error is not None


@pytest.mark.asyncio
async def test_unknown_call_site(router):
    result = await router.route_call("nonexistent_site", [{"role": "user", "content": "hi"}])
    assert result.success is False
    assert "unknown" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_fallback_emits_event(sample_config, breakers, cost_tracker, degradation):
    """When primary fails and fallback succeeds, a provider.fallback event is emitted."""
    from unittest.mock import AsyncMock

    event_bus = AsyncMock()
    delegate = MockDelegate(responses={
        "free-1": CallResult(success=False, error="model not found", status_code=404),
    })
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate, event_bus=event_bus,
    )
    result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])
    assert result.success is True
    assert result.provider_used == "free-2"
    assert result.fallback_used is True
    assert "free-1" in result.failed_providers

    # Verify fallback event was emitted
    fallback_calls = [
        c for c in event_bus.emit.call_args_list
        if c.args[2] == "provider.fallback"
    ]
    assert len(fallback_calls) == 1
    assert "free-1" in fallback_calls[0].args[3]  # message mentions failed provider


@pytest.mark.asyncio
async def test_no_fallback_event_when_primary_succeeds(sample_config, breakers, cost_tracker, degradation):
    """When primary succeeds, no provider.fallback event is emitted."""
    from unittest.mock import AsyncMock

    event_bus = AsyncMock()
    delegate = MockDelegate()
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate, event_bus=event_bus,
    )
    result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])
    assert result.success is True
    assert result.provider_used == "free-1"
    assert result.fallback_used is False
    assert result.failed_providers == ()

    fallback_calls = [
        c for c in event_bus.emit.call_args_list
        if c.args[2] == "provider.fallback"
    ]
    assert len(fallback_calls) == 0


@pytest.mark.asyncio
async def test_failed_providers_tracked_on_open_breaker(sample_config, breakers, cost_tracker, degradation):
    """Providers skipped due to open breaker are included in failed_providers."""
    # Trip free-1's breaker
    cb = breakers.get("free-1")
    for _ in range(3):
        cb.record_failure(ErrorCategory.TRANSIENT)

    delegate = MockDelegate()
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])
    assert result.success is True
    assert result.provider_used == "free-2"
    assert "free-1" in result.failed_providers
