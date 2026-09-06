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


@pytest.mark.asyncio
async def test_entitlement_403_fails_fast_and_chain_reaches_a_working_provider(
    sample_config, breakers, cost_tracker, degradation
):
    """ACCEPTANCE BAR — replay of the live outage this change exists for.

    `free-1` returns the verbatim 403 body measured from Mistral on
    2026-09-05. Before this change that message matched `_QUOTA_KEYWORDS`
    ("subscription") and classified QUOTA_EXHAUSTED, which is NOT in the
    router's fail-fast set — so the dead provider was retried with backoff,
    spending the chain's aggregate `max_total_s` budget before the walk could
    reach a provider that works.

    The load-bearing assertion is the ATTEMPT COUNT against the dead provider:
    exactly one. A chain that merely succeeds proves nothing, because the walk
    reaches `free-2` either way when the budget happens to be generous.
    """
    live_403 = CallResult(
        success=False,
        status_code=403,
        error=(
            '{"object":"error","message":"This model is not available in your '
            'subscription tier","type":"tier_not_allowed","code":"1910"}'
        ),
    )
    delegate = MockDelegate(responses={"free-1": live_403})
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )

    result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])

    dead_attempts = [c for c in delegate.calls if c["provider"] == "free-1"]
    assert len(dead_attempts) == 1, (
        f"entitlement 403 must not be retried against the same provider; "
        f"got {len(dead_attempts)} attempts"
    )
    assert result.success is True
    assert result.provider_used == "free-2"
    assert result.fallback_used is True
    # The breaker recorded it (unlike RATE_LIMITED/BAD_REQUEST) and holds the
    # long cap, so the next call skips the dead provider outright.
    cb = breakers.get("free-1")
    assert cb.last_failure_category == ErrorCategory.NOT_ENTITLED


@pytest.mark.asyncio
async def test_exhausted_quota_403_also_fails_fast(
    sample_config, breakers, cost_tracker, degradation
):
    """The other member of the same class, found by review rather than by the alert.

    A spent allowance is a BILLING state: it cannot clear inside a backoff
    window, so retrying it is the same defect as retrying an entitlement
    denial. It matters more than the single-provider case suggests, because
    the limit is usually account-global — OpenRouter's "Key limit exceeded
    (total limit)" applies to every openrouter entry in a chain at once, so a
    single walk could pay the wait repeatedly.

    MEASURED on this install 2026-09-05, before the fix: 4.1-6.8s average per
    exposure across 22 rows (openrouter-deepseek-v4 / -flash / -nemo).
    """
    live_quota_403 = CallResult(
        success=False,
        status_code=403,
        error=(
            "litellm.APIError: APIError: OpenrouterException - "
            '{"error":{"message":"Key limit exceeded (total limit)."}}'
        ),
    )
    delegate = MockDelegate(responses={"free-1": live_quota_403})
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )

    result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])

    assert len([c for c in delegate.calls if c["provider"] == "free-1"]) == 1
    assert result.success is True
    # Still classified QUOTA, not swept into the new category — the two keep
    # distinct meanings on every surface that reports them; only the retry
    # behaviour is now shared.
    assert breakers.get("free-1").last_failure_category == ErrorCategory.QUOTA_EXHAUSTED


@pytest.mark.asyncio
async def test_exhaustion_result_carries_the_providers_it_tried(
    sample_config, breakers, cost_tracker, degradation
):
    """The list is accumulated and then discarded on the failure path.

    `failed_providers` is populated for every skip and every failed call, and
    the SUCCESS path returns it — but the exhaustion path never did, so the one
    result whose reader most needs to know which providers were involved was
    the one that said nothing. Pure asymmetry, not a design decision.
    """
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
    assert result.failed_providers == ("paid-1", "paid-2")


@pytest.mark.asyncio
async def test_exhaustion_event_names_the_providers_and_the_chain_size(
    sample_config, breakers, cost_tracker, degradation
):
    """`attempts` alone is unreadable, and that is what the event carried.

    Two providers attempted out of a three-provider chain looks identical to
    two attempted out of two. The event now carries who was involved and how
    long the chain was, so `attempts` can be reconciled against both.
    """
    from unittest.mock import AsyncMock

    event_bus = AsyncMock()
    delegate = MockDelegate(responses={
        "free-1": CallResult(success=False, error="down", status_code=503),
        "free-2": CallResult(success=False, error="down", status_code=503),
        "paid-1": CallResult(success=False, error="down", status_code=503),
    })
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate, event_bus=event_bus,
    )
    result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])
    assert result.success is False

    exhausted = [
        c for c in event_bus.emit.call_args_list if c.args[2] == "all_exhausted"
    ]
    assert len(exhausted) == 1
    details = exhausted[0].kwargs
    assert details["failed_providers"] == ("free-1", "free-2", "paid-1")
    assert details["chain_size"] == 3
    assert details["attempts"] == 3
    # NOTE this case cannot discriminate `chain_size` from `attempts` — they
    # are equal here by construction. The sibling test below, where a skip
    # makes them differ, is what actually locks the field's meaning.


@pytest.mark.asyncio
async def test_a_breaker_skipped_provider_is_named_on_the_exhaustion_path(
    sample_config, breakers, cost_tracker, degradation
):
    """The motivating case: a provider skipped by an open breaker costs no
    attempt, so `attempts` under-counts the chain and the skipped name was the
    missing half of the explanation."""
    from unittest.mock import AsyncMock

    cb = breakers.get("free-1")
    for _ in range(3):
        cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.is_available() is False

    event_bus = AsyncMock()
    delegate = MockDelegate(responses={
        "free-2": CallResult(success=False, error="down", status_code=503),
        "paid-1": CallResult(success=False, error="down", status_code=503),
    })
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate, event_bus=event_bus,
    )
    result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])
    assert result.success is False
    assert "free-1" in result.failed_providers
    exhausted = [
        c for c in event_bus.emit.call_args_list if c.args[2] == "all_exhausted"
    ]
    details = exhausted[0].kwargs
    assert "free-1" in details["failed_providers"]
    # attempts under-counts the chain BECAUSE of the skip — which is exactly
    # what the chain size makes legible rather than mysterious. Exact values,
    # not just the inequality: `<` alone is also satisfied by chain_size=99.
    assert details["attempts"] == 2
    assert details["chain_size"] == 3
    assert details["attempts"] < details["chain_size"]


@pytest.mark.asyncio
async def test_chain_size_counts_the_walkable_chain_not_the_configured_one(
    sample_config, breakers, cost_tracker, degradation
):
    """`chain_size` is the post-`_filter_chain` length, and that is the only
    number `attempts` can be reconciled against — a `never_pays` site never
    walks its paid entries, so counting them would make every such exhaustion
    look like it stopped early when it did not.

    Locked because it is invisible otherwise: substituting the CONFIGURED
    length survived this entire file green, since no fixture made the filter
    drop anything. It is not hypothetical either — on the live install three
    of nine `never_pays` call sites currently diverge, all because a provider
    moved from free to paid.
    """
    from unittest.mock import AsyncMock

    event_bus = AsyncMock()
    delegate = MockDelegate(responses={
        "free-1": CallResult(success=False, error="down", status_code=503),
        "free-2": CallResult(success=False, error="down", status_code=503),
    })
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate, event_bus=event_bus,
    )
    site = sample_config.call_sites["test_never_pays"]
    assert len(site.chain) == 3, "the fixture must give the filter something to drop"

    result = await router.route_call("test_never_pays", [{"role": "user", "content": "hi"}])
    assert result.success is False
    assert "paid-1" not in result.failed_providers

    details = [
        c for c in event_bus.emit.call_args_list if c.args[2] == "all_exhausted"
    ][0].kwargs
    assert details["chain_size"] == 2, "counted the configured chain, not the walkable one"
    assert details["attempts"] == 2


@pytest.mark.asyncio
async def test_the_exhaustion_LOG_LINE_names_the_providers(
    sample_config, breakers, cost_tracker, degradation, caplog
):
    """THE surface the runbook sends a reader to, and the one details never reach.

    `GenesisEventBus.emit` logs `subsystem=… event=… msg=…` and nothing more, so
    everything passed as a detail lands in the persisted event and the dashboard
    while `journalctl` shows the same line it always showed. Attaching the names
    as details ALONE would have left this PR's own stated surface byte-identical
    to the behaviour it exists to fix.

    So this asserts through a REAL event bus and the stdlib logger rather than a
    mock: what is being checked is that the text arrives at the log, and a mock
    cannot fail that way.
    """
    import logging

    from genesis.observability.events import GenesisEventBus

    delegate = MockDelegate(responses={
        "free-1": CallResult(success=False, error="down", status_code=503),
        "free-2": CallResult(success=False, error="down", status_code=503),
        "paid-1": CallResult(success=False, error="down", status_code=503),
    })
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate, event_bus=GenesisEventBus(),
    )
    with caplog.at_level(logging.ERROR, logger="genesis.observability.events"):
        result = await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])
    assert result.success is False

    lines = [r.getMessage() for r in caplog.records if "all_exhausted" in r.getMessage()]
    assert len(lines) == 1, f"expected one exhaustion log line, got {lines}"
    line = lines[0]
    for name in ("free-1", "free-2", "paid-1"):
        assert name in line, f"{name} never reached the log: {line}"
    assert "test_mixed" in line, "the call site is what a reader greps for first"


@pytest.mark.asyncio
async def test_the_exhaustion_message_carries_both_counts(
    sample_config, breakers, cost_tracker, degradation
):
    """Names alone still cannot distinguish "tried two of two" from "tried two of
    seven" — the skipped members have no names to print. Both numbers travel with
    them, in the message, for the same reason the names do."""
    from unittest.mock import AsyncMock

    cb = breakers.get("free-1")
    for _ in range(3):
        cb.record_failure(ErrorCategory.TRANSIENT)

    event_bus = AsyncMock()
    delegate = MockDelegate(responses={
        "free-2": CallResult(success=False, error="down", status_code=503),
        "paid-1": CallResult(success=False, error="down", status_code=503),
    })
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate, event_bus=event_bus,
    )
    await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])
    message = [
        c for c in event_bus.emit.call_args_list if c.args[2] == "all_exhausted"
    ][0].args[3]
    assert "2 attempted of 3 walkable" in message, message
    assert "free-1" in message, "the breaker-skipped provider is the point of the event"
    # …and it is labelled a SKIP with its reason, not a failure: it was never
    # called, and `failed` would read as an outage where there is none.
    assert "skipped: free-1 (breaker open)" in message, message
    assert "free-1" not in message.split("skipped:")[0], (
        "the failed clause absorbed a provider that was never called"
    )
    assert "failed: free-2, paid-1" in message, message


@pytest.mark.asyncio
async def test_exhaustion_grouping_prefix_is_stable_across_states(
    sample_config, breakers, cost_tracker, degradation
):
    """The Errors dashboard groups events by the first MSG_GROUP_PREFIX_LEN
    characters of the message and keys MANUAL RESOLUTIONS off that prefix
    (db/crud/events.py SUBSTR + dashboard/routes/errors.py). If the prefix
    varies with breaker/key/budget state, one recurring outage splits into a
    group per permutation and a resolved group resurrects under a new key.
    So: same call site, two different exhaustion states → identical prefix;
    the per-occurrence diagnostics live entirely beyond it.
    """
    from unittest.mock import AsyncMock

    from genesis.db.crud.events import MSG_GROUP_PREFIX_LEN

    def exhausted_message(bus):
        return [
            c for c in bus.emit.call_args_list if c.args[2] == "all_exhausted"
        ][0].args[3]

    bus_a = AsyncMock()
    delegate = MockDelegate(responses={
        "free-1": CallResult(success=False, error="down", status_code=503),
        "free-2": CallResult(success=False, error="down", status_code=503),
        "paid-1": CallResult(success=False, error="down", status_code=503),
    })
    router = Router(
        config=sample_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate, event_bus=bus_a,
    )
    await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])

    # Second exhaustion of the SAME site under a different state: a breaker
    # now skips free-1, changing attempts and the failed/skipped clauses.
    cb = breakers.get("free-1")
    for _ in range(3):
        cb.record_failure(ErrorCategory.TRANSIENT)
    bus_b = AsyncMock()
    router._event_bus = bus_b
    await router.route_call("test_mixed", [{"role": "user", "content": "hi"}])

    msg_a, msg_b = exhausted_message(bus_a), exhausted_message(bus_b)
    assert msg_a != msg_b, "the states must actually differ for this to lock anything"
    assert msg_a[:MSG_GROUP_PREFIX_LEN] == msg_b[:MSG_GROUP_PREFIX_LEN], (
        f"grouping prefix varies with exhaustion state:\n{msg_a!r}\n{msg_b!r}"
    )
    assert "test_mixed" in msg_a[:MSG_GROUP_PREFIX_LEN], (
        "the call site is the group's identity and must sit inside the prefix"
    )


def test_nothing_recorded_prints_no_dangling_clause():
    """EMPTY IS A REAL STATE. Only the aggregate deadline abandons the walk while
    recording nothing — `failed: ` or `skipped: ` with nothing after it reads as
    a formatting bug, and the `0 attempted of N` counts already tell that story
    on their own.
    """
    from genesis.routing.router import _exhaustion_clause

    assert _exhaustion_clause([], []) == ""


def test_a_skipped_provider_is_not_labelled_failed():
    """A provider passed over for a missing key, an open breaker, or a spent
    budget was never CALLED — printing it under `failed:` reads as an outage
    where there may be none. Missing keys are the normal state of a fresh
    install, and a budget gate is a decision, not a fault. Each skip carries
    its reason so the operator can tell those states apart from the log line
    alone."""
    from genesis.routing.router import _exhaustion_clause

    clause = _exhaustion_clause(
        ["really-down"], [("unconfigured", "no API key"), ("gated", "budget exceeded")]
    )
    assert "failed: really-down" in clause
    assert "skipped: unconfigured (no API key), gated (budget exceeded)" in clause
    # The failed clause must not absorb the skipped names.
    failed_part = clause.split("skipped:")[0]
    assert "unconfigured" not in failed_part
    assert "gated" not in failed_part


def test_a_long_chain_summarises_instead_of_flooding_one_log_line():
    """A bound on an ERROR line that goes to journalctl. Chains are single digits
    today, so this never trims in practice — it is here so a future long chain
    cannot turn one line into a paragraph. Applied per clause, so a long failed
    list cannot squeeze out the skipped one or vice versa."""
    from genesis.routing.router import _FAILED_NAMES_IN_MESSAGE, _exhaustion_clause

    names = [f"p-{i}" for i in range(_FAILED_NAMES_IN_MESSAGE + 3)]
    clause = _exhaustion_clause(names, [])
    assert clause.count("p-") == _FAILED_NAMES_IN_MESSAGE, clause
    assert "+3 more" in clause, "the trimmed count is what stops it reading as complete"
    assert names[-1] not in clause
    assert names[0] in clause, "the FIRST provider tried is the one to keep"
