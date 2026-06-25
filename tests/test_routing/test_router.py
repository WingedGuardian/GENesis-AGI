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
async def test_timeout_fails_fast_no_same_provider_retry(
    sample_config, breakers, cost_tracker, degradation,
):
    """A timeout (408) must NOT be retried against the same provider.

    A hung provider won't un-hang on an immediate retry — retrying just
    multiplies the timeout wall-clock (the 30-min dream-cycle hangs). The
    router fails fast to the next provider, but the circuit breaker still
    records the failure so a repeatedly-hanging provider trips OPEN.
    """
    delegate = MockDelegate(responses={
        "free-1": CallResult(
            success=False,
            error="litellm.Timeout: request timed out",
            status_code=408,
        ),
    })
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])

    # Failed fast to the next provider
    assert result.success is True
    assert result.provider_used == "free-2"
    assert "free-1" in result.failed_providers
    # free-1 called exactly ONCE — not retried despite default max_retries=1
    free1_calls = [c for c in delegate.calls if c["provider"] == "free-1"]
    assert len(free1_calls) == 1
    # The timeout is still recorded against free-1's circuit breaker
    assert breakers.get("free-1").consecutive_failures == 1


@pytest.mark.asyncio
async def test_rate_limited_fails_fast_no_retry_no_breaker_trip(
    sample_config, breakers, cost_tracker, degradation,
):
    """A 429 (RATE_LIMITED) must fail fast to the next provider WITHOUT a
    same-provider retry AND WITHOUT tripping the breaker. A rate limit is
    expected backpressure (the rate gate is the right brake) — tripping the
    breaker would wrongly take a reachable provider offline for every other
    call site that uses it.
    """
    delegate = MockDelegate(responses={
        "free-1": CallResult(success=False, error="rate limited", status_code=429),
    })
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])

    assert result.success is True
    assert result.provider_used == "free-2"
    assert "free-1" in result.failed_providers
    # Called exactly ONCE — not retried despite default max_retries=1
    free1_calls = [c for c in delegate.calls if c["provider"] == "free-1"]
    assert len(free1_calls) == 1
    # The breaker must NOT record the rate-limit as a failure
    assert breakers.get("free-1").consecutive_failures == 0
    assert breakers.get("free-1").trip_count == 0


@pytest.mark.asyncio
async def test_bad_request_fails_fast_no_retry_no_breaker_trip(
    sample_config, breakers, cost_tracker, degradation,
):
    """A 400 (BAD_REQUEST: context overflow / content policy / malformed) must
    fail fast WITHOUT a same-provider retry AND WITHOUT tripping the breaker —
    the error is our payload's fault, not the provider being unhealthy.
    """
    delegate = MockDelegate(responses={
        "free-1": CallResult(
            success=False, error="context window exceeded", status_code=400,
        ),
    })
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])

    assert result.success is True
    assert result.provider_used == "free-2"
    free1_calls = [c for c in delegate.calls if c["provider"] == "free-1"]
    assert len(free1_calls) == 1
    assert breakers.get("free-1").consecutive_failures == 0
    assert breakers.get("free-1").trip_count == 0


@pytest.mark.asyncio
async def test_transient_still_retries_and_records_breaker_failure(
    sample_config, breakers, cost_tracker, degradation,
):
    """Contrast guard: a 503 (TRANSIENT) is STILL retried on the same provider
    (default max_retries=1 → 2 calls) and STILL recorded against the breaker —
    proving the RATE_LIMITED/BAD_REQUEST no-trip gating did not disable real
    health failures.
    """
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
    # Retried once on free-1 (2 calls total) since 503 is transient
    free1_calls = [c for c in delegate.calls if c["provider"] == "free-1"]
    assert len(free1_calls) == 2
    # And the breaker recorded the failure (one record per provider per route_call)
    assert breakers.get("free-1").consecutive_failures == 1


@pytest.mark.asyncio
async def test_timeout_failover_end_to_end(sample_config, breakers, cost_tracker, degradation):
    """E2E: a hung provider routed through the REAL LiteLLMDelegate is
    hard-capped by asyncio.wait_for and the router fails fast to the next
    provider — proving the delegate cap (Change 2) and the router fail-fast
    (Change 1) compose correctly, within the timeout wall-clock (not 5x it).
    """
    import asyncio
    import time
    from types import SimpleNamespace
    from unittest.mock import patch

    import genesis.routing.litellm_delegate as ld
    from genesis.routing.litellm_delegate import LiteLLMDelegate

    async def _branching(*args, **kwargs):
        # free-1 is a mistral provider — make it hang past the timeout.
        if "mistral" in kwargs.get("model", ""):
            await asyncio.sleep(2.0)
        # free-2 (groq) responds normally.
        usage = SimpleNamespace(prompt_tokens=5, completion_tokens=3)
        message = SimpleNamespace(content="ok")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

    delegate = LiteLLMDelegate(sample_config)
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )

    with (
        patch.object(ld, "_DEFAULT_TIMEOUT_S", 0.3),
        patch("genesis.routing.litellm_delegate.litellm") as mock_litellm,
    ):
        mock_litellm.acompletion = _branching
        start = time.monotonic()
        result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])
        elapsed = time.monotonic() - start

    # Failed over from the hung free-1 to free-2
    assert result.success is True
    assert result.provider_used == "free-2"
    assert "free-1" in result.failed_providers
    # Capped near the 0.3s timeout, NOT the 2s hang (and not retried 5x)
    assert elapsed < 1.5


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


# ── Aggregate wall-clock deadline (RetryPolicy.max_total_s) ───────────────────


def _three_free_config(max_total_s, max_retries=0):
    from genesis.routing.types import CallSiteConfig, ProviderConfig, RetryPolicy, RoutingConfig
    providers = {
        f"free-{i}": ProviderConfig(
            name=f"free-{i}", provider_type="test", model_id="m",
            is_free=True, rpm_limit=None, open_duration_s=120,
        ) for i in range(1, 4)
    }
    return RoutingConfig(
        providers=providers,
        call_sites={"site": CallSiteConfig(id="site", chain=["free-1", "free-2", "free-3"])},
        retry_profiles={"default": RetryPolicy(
            max_retries=max_retries, base_delay_ms=0, jitter_pct=0.0, max_total_s=max_total_s,
        )},
    )


def _clock_advancing_delegate(clock, per_call=0.1):
    """A MockDelegate that fails every provider and advances `clock` per call."""
    delegate = MockDelegate(responses={
        f"free-{i}": CallResult(success=False, status_code=503, error="down")
        for i in range(1, 4)
    })
    orig = delegate.call

    async def _timed(provider, model_id, messages, **kwargs):
        r = await orig(provider, model_id, messages, **kwargs)
        clock[0] += per_call
        return r

    delegate.call = _timed
    return delegate


@pytest.mark.asyncio
async def test_aggregate_deadline_stops_chain_walk(cost_tracker, degradation):
    """With max_total_s set, route_call stops walking the chain once the
    aggregate wall-clock budget is exceeded — it does NOT try every remaining
    provider. The gate is checked BETWEEN providers (never interrupts a call).
    """
    from unittest.mock import patch

    config = _three_free_config(max_total_s=0.15)
    breakers = CircuitBreakerRegistry(config.providers)
    clock = [0.0]
    delegate = _clock_advancing_delegate(clock)
    router = Router(
        config=config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )

    with patch("genesis.routing.router.time.monotonic", lambda: clock[0]):
        result = await router.route_call("site", [{"role": "user", "content": "hi"}])

    assert result.success is False
    # free-1 (0.0→0.1) + free-2 (0.1→0.2); at the top of free-3 the elapsed
    # (0.2) already exceeds max_total_s (0.15) → free-3 is never attempted.
    called = [c["provider"] for c in delegate.calls]
    assert called == ["free-1", "free-2"]
    assert "free-3" not in called


@pytest.mark.asyncio
async def test_no_aggregate_deadline_tries_full_chain(cost_tracker, degradation):
    """max_total_s=None (default) keeps today's behavior — the whole chain is
    attempted no matter how long the cumulative wall-clock is.
    """
    from unittest.mock import patch

    config = _three_free_config(max_total_s=None)
    breakers = CircuitBreakerRegistry(config.providers)
    clock = [0.0]
    delegate = _clock_advancing_delegate(clock, per_call=10.0)  # huge per-call
    router = Router(
        config=config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )

    with patch("genesis.routing.router.time.monotonic", lambda: clock[0]):
        result = await router.route_call("site", [{"role": "user", "content": "hi"}])

    assert result.success is False
    called = [c["provider"] for c in delegate.calls]
    assert called == ["free-1", "free-2", "free-3"]


@pytest.mark.asyncio
async def test_aggregate_deadline_stops_inner_retries(cost_tracker, degradation):
    """The deadline also bounds same-provider RETRIES: a single provider with a
    long retry budget stops retrying once the aggregate deadline passes (never
    interrupting an in-flight attempt — checked between attempts).
    """
    from unittest.mock import patch

    from genesis.routing.types import CallSiteConfig, ProviderConfig, RetryPolicy, RoutingConfig

    providers = {"free-1": ProviderConfig(
        name="free-1", provider_type="test", model_id="m",
        is_free=True, rpm_limit=None, open_duration_s=120,
    )}
    config = RoutingConfig(
        providers=providers,
        call_sites={"site": CallSiteConfig(id="site", chain=["free-1"])},
        retry_profiles={"default": RetryPolicy(
            max_retries=5, base_delay_ms=0, jitter_pct=0.0, max_total_s=0.25,
        )},
    )
    breakers = CircuitBreakerRegistry(providers)
    clock = [0.0]
    delegate = _clock_advancing_delegate(clock)  # advances 0.1 per call

    router = Router(
        config=config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    with patch("genesis.routing.router.time.monotonic", lambda: clock[0]):
        result = await router.route_call("site", [{"role": "user", "content": "hi"}])

    assert result.success is False
    # attempt0(0.0→0.1) attempt1(0.1→0.2) attempt2(0.2→0.3); attempt3 sees
    # 0.3 >= 0.25 and stops — 3 calls, NOT the full 6 (max_retries+1).
    assert len(delegate.calls) == 3
