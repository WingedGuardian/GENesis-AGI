"""Tests for Router auto-dead-letter on chain exhaustion."""

from __future__ import annotations

import pytest

from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.cost_tracker import CostTracker
from genesis.routing.dead_letter import DeadLetterQueue
from genesis.routing.degradation import DegradationTracker
from genesis.routing.router import Router
from genesis.routing.types import CallResult

from .conftest import MockDelegate


@pytest.fixture
def all_fail_delegate():
    return MockDelegate(responses={
        "paid-1": CallResult(success=False, error="down", status_code=503),
        "paid-2": CallResult(success=False, error="down", status_code=503),
    })


@pytest.fixture
def dead_letter(db):
    return DeadLetterQueue(db)


@pytest.mark.asyncio
async def test_chain_exhaustion_with_dead_letter(
    sample_config, sample_providers, db, all_fail_delegate, dead_letter,
):
    """When all providers fail and dead_letter is set, item is enqueued."""
    breakers = CircuitBreakerRegistry(sample_providers)
    cost_tracker = CostTracker(db)
    degradation = DegradationTracker()

    router = Router(
        config=sample_config,
        breakers=breakers,
        cost_tracker=cost_tracker,
        degradation=degradation,
        delegate=all_fail_delegate,
        dead_letter=dead_letter,
    )

    result = await router.route_call("test_paid", [{"role": "user", "content": "hi"}])
    assert result.success is False
    assert result.dead_lettered is True

    count = await dead_letter.get_pending_count()
    assert count == 1


@pytest.mark.asyncio
async def test_chain_exhaustion_without_dead_letter(
    sample_config, sample_providers, db, all_fail_delegate,
):
    """Without dead_letter, existing behavior unchanged and dead_lettered=False."""
    breakers = CircuitBreakerRegistry(sample_providers)
    cost_tracker = CostTracker(db)
    degradation = DegradationTracker()

    router = Router(
        config=sample_config,
        breakers=breakers,
        cost_tracker=cost_tracker,
        degradation=degradation,
        delegate=all_fail_delegate,
    )

    result = await router.route_call("test_paid", [{"role": "user", "content": "hi"}])
    assert result.success is False
    assert result.dead_lettered is False
