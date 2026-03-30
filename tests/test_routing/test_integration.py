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
    assert result.provider_used == "mistral-free"  # first in chain
    assert result.fallback_used is False
    assert len(delegate.calls) == 1


@pytest.mark.asyncio
async def test_full_stack_fallback_chain(real_config, breakers, cost_tracker, degradation):
    """mistral-free fails, should fallback to groq-free for 3_micro_reflection."""
    delegate = MockDelegate(responses={
        "mistral-free": CallResult(success=False, error="rate limited", status_code=429),
    })
    router = Router(
        config=real_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("3_micro_reflection", [{"role": "user", "content": "reflect"}])
    assert result.success is True
    assert result.provider_used == "groq-free"
    assert result.fallback_used is True


@pytest.mark.asyncio
async def test_surplus_never_pays(real_config, breakers, cost_tracker, degradation):
    """12_surplus_brainstorm has never_pays — all free fail, no paid providers called."""
    delegate = MockDelegate(responses={
        "mistral-free": CallResult(success=False, error="down", status_code=503),
        "groq-free": CallResult(success=False, error="down", status_code=503),
        "gemini-free": CallResult(success=False, error="down", status_code=503),
        "openrouter-free": CallResult(success=False, error="down", status_code=503),
    })
    router = Router(
        config=real_config, breakers=breakers, cost_tracker=cost_tracker,
        degradation=degradation, delegate=delegate,
    )
    result = await router.route_call("12_surplus_brainstorm", [{"role": "user", "content": "brainstorm"}])
    assert result.success is False
    providers_called = {c["provider"] for c in delegate.calls}
    # Only free providers
    for p in providers_called:
        assert real_config.providers[p].is_free


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
    """Trip groq-free breaker, route 3_micro_reflection — should use mistral-free."""
    cb = breakers.get("groq-free")
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
    assert result.provider_used == "mistral-free"
    assert all(c["provider"] != "groq-free" for c in delegate.calls)
