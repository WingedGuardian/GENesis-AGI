"""Integration tests — real config/model_routing.yaml + MockDelegate through full stack."""

from pathlib import Path

import pytest

from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.config import load_config
from genesis.routing.cost_tracker import CostTracker
from genesis.routing.dead_letter import DeadLetterQueue
from genesis.routing.degradation import DegradationTracker
from genesis.routing.router import Router
from genesis.routing.types import CallResult, ErrorCategory

from .conftest import MockDelegate

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "model_routing.yaml"


@pytest.fixture
def real_config():
    return load_config(CONFIG_PATH)


@pytest.fixture
def breakers(real_config):
    return CircuitBreakerRegistry(real_config.providers)


@pytest.fixture
def degradation():
    return DegradationTracker()


@pytest.fixture
async def cost_tracker(db):
    return CostTracker(db)


@pytest.fixture
async def dlq(db):
    return DeadLetterQueue(db)


@pytest.mark.asyncio
async def test_full_stack_success(real_config, breakers, cost_tracker, degradation):
    """Route 3_micro_reflection through the full stack — first provider succeeds."""
    delegate = MockDelegate()
    router = Router(
        config=real_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("3_micro_reflection", [{"role": "user", "content": "reflect"}])
    assert result.success is True
    assert result.provider_used == "mistral-small-free"  # first in chain
    assert result.fallback_used is False
    assert len(delegate.calls) == 1


@pytest.mark.asyncio
async def test_full_stack_fallback_chain(real_config, breakers, cost_tracker, degradation):
    """mistral-small-free fails, should fallback to openrouter-free for 3_micro_reflection."""
    delegate = MockDelegate(responses={
        "mistral-small-free": CallResult(success=False, error="rate limited", status_code=429),
    })
    router = Router(
        config=real_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("3_micro_reflection", [{"role": "user", "content": "reflect"}])
    assert result.success is True
    assert result.provider_used == "openrouter-free"
    assert result.fallback_used is True


@pytest.mark.asyncio
async def test_surplus_never_pays(real_config, breakers, cost_tracker, degradation):
    """12_surplus_brainstorm has never_pays — all free fail, no paid providers called.

    The down-list is DERIVED from the site's own free chain rather than
    hard-coded. It used to be an enumerated list of provider names, which made
    the test a tripwire on the roster instead of on the invariant: adding any
    free rung to this chain left that rung un-mocked, so `MockDelegate` handed
    it the default success stub and the run reached `assert result.success is
    False` with a success. That reads as a `never_pays` breach and is not one —
    the invariant is enforced structurally by `Router._filter_chain`, which
    walks only providers flagged `free`.

    What catches a paid provider leaking through is the combination of
    `MockDelegate`'s default SUCCESS stub and `assert result.success is False`
    — an unfiltered walk reaches `mistral-large-free` (named `-free`, flagged
    `free: false` since Mistral pulled Large from the free tier), which is not
    in `responses`, so it succeeds and the assertion fires. The trailing loop
    is a secondary, explicit check on WHICH providers were walked; it is not
    reached in that failure mode.
    """
    site = real_config.call_sites["12_surplus_brainstorm"]
    free_chain = [p for p in site.chain if real_config.providers[p].is_free]
    assert free_chain, "guard-the-guard: a site with no free rung cannot test never_pays"
    delegate = MockDelegate(responses={
        p: CallResult(success=False, error="down", status_code=503) for p in free_chain
    })
    router = Router(
        config=real_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("12_surplus_brainstorm", [{"role": "user", "content": "brainstorm"}])
    assert result.success is False
    providers_called = {c["provider"] for c in delegate.calls}
    # This single assertion carries both halves of the invariant, which is why
    # there is no separate per-provider `is_free` loop after it:
    #   - EVERY free rung was tried. Without this, `_filter_chain` returning []
    #     short-circuits `route_call` before any attempt and `success is False`
    #     passes because nothing ran — a worse bug than the one under test.
    #   - ONLY free rungs were tried, since `free_chain` is itself the `is_free`
    #     filter. A separate loop over `providers_called` asserting `is_free`
    #     could no longer fail once this equality holds; it would imply
    #     independent coverage it does not provide.
    assert providers_called == set(free_chain), (
        f"expected every free rung to be tried; walked {sorted(providers_called)} "
        f"of {sorted(free_chain)}"
    )


@pytest.mark.asyncio
async def test_dead_letter_queue_lifecycle(dlq):
    """enqueue → count → replay → verify."""
    await dlq.enqueue("llm_call", {"msg": "test"}, "groq-free", "503 error")
    assert await dlq.get_pending_count() == 1
    assert await dlq.get_pending_count(target_provider="groq-free") == 1

    replayed = await dlq.replay_pending("groq-free")
    assert replayed == 1
    assert await dlq.get_pending_count() == 0


@pytest.mark.asyncio
async def test_circuit_breaker_affects_routing(real_config, breakers, cost_tracker, degradation):
    """Trip mistral-small-free breaker, route 3_micro_reflection — should use openrouter-free."""
    cb = breakers.get("mistral-small-free")
    for _ in range(3):
        cb.record_failure(ErrorCategory.TRANSIENT)
    assert not cb.is_available()

    delegate = MockDelegate()
    router = Router(
        config=real_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("3_micro_reflection", [{"role": "user", "content": "reflect"}])
    assert result.success is True
    assert result.provider_used == "openrouter-free"
    assert all(c["provider"] != "mistral-small-free" for c in delegate.calls)
