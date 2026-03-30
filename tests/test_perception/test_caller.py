"""Tests for LLMCaller — routes through genesis.routing."""

from __future__ import annotations

from unittest.mock import AsyncMock

from genesis.perception.types import LLMResponse
from genesis.routing.types import RoutingResult


def _success_result():
    return RoutingResult(
        success=True,
        call_site_id="3_micro_reflection",
        provider_used="groq-free",
        model_id="llama-3.3-70b-versatile",
        content='{"tags": ["idle"], "salience": 0.1, "anomaly": false, "summary": "Normal.", "signals_examined": 5}',
        attempts=1,
    )


def _failure_result():
    return RoutingResult(
        success=False,
        call_site_id="3_micro_reflection",
        error="all providers exhausted",
        dead_lettered=True,
    )


async def test_call_success():
    from genesis.perception.caller import LLMCaller

    router = AsyncMock()
    router.route_call = AsyncMock(return_value=_success_result())
    caller = LLMCaller(router=router)
    result = await caller.call("Test prompt", call_site_id="3_micro_reflection")

    assert result is not None
    assert isinstance(result, LLMResponse)
    assert result.model == "groq-free"
    assert "idle" in result.text
    router.route_call.assert_called_once()


async def test_call_chain_exhausted():
    from genesis.perception.caller import LLMCaller

    router = AsyncMock()
    router.route_call = AsyncMock(return_value=_failure_result())
    caller = LLMCaller(router=router)
    result = await caller.call("Test prompt", call_site_id="3_micro_reflection")

    assert result is None


async def test_call_passes_messages_correctly():
    from genesis.perception.caller import LLMCaller

    router = AsyncMock()
    router.route_call = AsyncMock(return_value=_success_result())
    caller = LLMCaller(router=router)
    await caller.call("My prompt text", call_site_id="3_micro_reflection")

    args = router.route_call.call_args
    assert args[0][0] == "3_micro_reflection"
    assert args[0][1] == [{"role": "user", "content": "My prompt text"}]


async def test_call_emits_event_on_failure():
    from genesis.observability.events import GenesisEventBus
    from genesis.perception.caller import LLMCaller

    bus = GenesisEventBus()
    events = []

    async def listener(e):
        events.append(e)

    bus.subscribe(listener)

    router = AsyncMock()
    router.route_call = AsyncMock(return_value=_failure_result())
    caller = LLMCaller(router=router, event_bus=bus)
    await caller.call("Test prompt", call_site_id="3_micro_reflection")

    assert len(events) == 1
    assert events[0].event_type == "reflection.call_failed"
