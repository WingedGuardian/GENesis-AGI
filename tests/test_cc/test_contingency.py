"""Tests for CC contingency routing — API fallback when CC is unavailable."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.awareness.types import Depth
from genesis.cc.contingency import CCContingencyDispatcher, ContingencyResult


def _make_router(success: bool = True, content: str = "response", error: str = ""):
    """Create a mock Router with route_call returning a RoutingResult-like."""
    router = AsyncMock()
    result = MagicMock()
    result.success = success
    result.content = content if success else None
    result.provider_used = "test-provider" if success else None
    result.error = error if not success else None
    router.route_call = AsyncMock(return_value=result)
    return router


def _make_deferred_queue():
    """Create a mock DeferredWorkQueue."""
    q = AsyncMock()
    q.enqueue = AsyncMock(return_value="item-uuid-123")
    return q


class TestReflectionDispatch:
    """Contingency reflection routing."""

    @pytest.mark.asyncio
    async def test_deep_reflection_defers(self) -> None:
        """Deep reflections defer instead of routing to inferior models."""
        router = _make_router()
        deferred = _make_deferred_queue()
        dispatcher = CCContingencyDispatcher(
            router=router, deferred_queue=deferred,
        )

        result = await dispatcher.dispatch_reflection(
            Depth.DEEP, "Analyze signals", "You are Genesis.",
        )

        assert not result.success
        assert result.deferred
        assert "deferred" in result.reason.lower()
        deferred.enqueue.assert_called_once()
        call_kwargs = deferred.enqueue.call_args[1]
        assert call_kwargs["staleness_policy"] == "ttl"
        assert call_kwargs["staleness_ttl_s"] == 14400
        router.route_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_light_reflection_defers(self) -> None:
        """Light reflections defer instead of routing to inferior models."""
        router = _make_router()
        deferred = _make_deferred_queue()
        dispatcher = CCContingencyDispatcher(
            router=router, deferred_queue=deferred,
        )

        result = await dispatcher.dispatch_reflection(
            Depth.LIGHT, "Quick check", "System prompt",
        )

        assert not result.success
        assert result.deferred
        assert "deferred" in result.reason.lower()
        deferred.enqueue.assert_called_once()
        router.route_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_strategic_reflection_defers(self) -> None:
        router = _make_router()
        deferred = _make_deferred_queue()
        dispatcher = CCContingencyDispatcher(
            router=router, deferred_queue=deferred,
        )

        result = await dispatcher.dispatch_reflection(
            Depth.STRATEGIC, "Strategic review", "You are Genesis.",
        )

        assert not result.success
        assert result.deferred
        assert "deferred" in result.reason.lower()
        deferred.enqueue.assert_called_once()
        call_kwargs = deferred.enqueue.call_args[1]
        assert call_kwargs["staleness_policy"] == "ttl"
        assert call_kwargs["staleness_ttl_s"] == 14400
        router.route_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_deferred_without_queue(self) -> None:
        """Defers even without a deferred queue (just returns failure)."""
        for depth in (Depth.STRATEGIC, Depth.DEEP, Depth.LIGHT):
            dispatcher = CCContingencyDispatcher(router=_make_router())
            result = await dispatcher.dispatch_reflection(
                depth, "Review", "System prompt",
            )
            assert not result.success
            assert result.deferred

    @pytest.mark.asyncio
    async def test_micro_reflection_routes_through_api(self) -> None:
        """Micro reflections still route to contingency (free models, quality not critical)."""
        router = _make_router(content="Micro result")
        dispatcher = CCContingencyDispatcher(router=router)

        result = await dispatcher.dispatch_reflection(
            Depth.MICRO, "Quick triage", "System prompt",
        )

        assert result.success
        assert result.content == "Micro result"
        router.route_call.assert_called_once()
        call_args = router.route_call.call_args
        assert call_args[0][0] == "contingency_inbox"

    @pytest.mark.asyncio
    async def test_micro_reflection_api_failure(self) -> None:
        router = _make_router(success=False, error="All providers failed")
        dispatcher = CCContingencyDispatcher(router=router)

        result = await dispatcher.dispatch_reflection(
            Depth.MICRO, "Triage", "System prompt",
        )

        assert not result.success
        assert "failed" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_micro_reflection_router_exception(self) -> None:
        router = AsyncMock()
        router.route_call = AsyncMock(side_effect=ConnectionError("down"))
        dispatcher = CCContingencyDispatcher(router=router)

        result = await dispatcher.dispatch_reflection(
            Depth.MICRO, "Triage", "System prompt",
        )

        assert not result.success
        assert "failed" in result.reason.lower()


class TestConversationDispatch:
    """Contingency foreground conversation routing."""

    @pytest.mark.asyncio
    async def test_conversation_routes_through_api(self) -> None:
        router = _make_router(content="Hello! I'm running in API mode.")
        dispatcher = CCContingencyDispatcher(router=router)

        result = await dispatcher.dispatch_conversation(
            [{"role": "user", "content": "Hello"}],
            "You are Genesis.",
        )

        assert result.success
        assert "API mode" in result.content
        call_args = router.route_call.call_args
        assert call_args[0][0] == "contingency_foreground"
        # System prompt should be prepended
        messages = call_args[0][1]
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    @pytest.mark.asyncio
    async def test_conversation_failure(self) -> None:
        router = _make_router(success=False, error="No providers")
        dispatcher = CCContingencyDispatcher(router=router)

        result = await dispatcher.dispatch_conversation(
            [{"role": "user", "content": "Hello"}],
            "System prompt",
        )

        assert not result.success

    @pytest.mark.asyncio
    async def test_conversation_preserves_history(self) -> None:
        """Multiple messages in history are passed through."""
        router = _make_router(content="Continued conversation")
        dispatcher = CCContingencyDispatcher(router=router)

        history = [
            {"role": "user", "content": "First message"},
            {"role": "assistant", "content": "First response"},
            {"role": "user", "content": "Second message"},
        ]
        await dispatcher.dispatch_conversation(history, "System prompt")

        messages = router.route_call.call_args[0][1]
        assert len(messages) == 4  # system + 3 history


class TestInboxDispatch:
    """Contingency inbox evaluation routing."""

    @pytest.mark.asyncio
    async def test_inbox_routes_through_free_api(self) -> None:
        router = _make_router(content='{"items": []}')
        dispatcher = CCContingencyDispatcher(router=router)

        result = await dispatcher.dispatch_inbox(
            "Evaluate these items", "Inbox system prompt",
        )

        assert result.success
        call_args = router.route_call.call_args
        assert call_args[0][0] == "contingency_inbox"

    @pytest.mark.asyncio
    async def test_inbox_failure(self) -> None:
        router = _make_router(success=False, error="All free providers down")
        dispatcher = CCContingencyDispatcher(router=router)

        result = await dispatcher.dispatch_inbox("Evaluate", "System prompt")

        assert not result.success


class TestContingencyResult:
    """ContingencyResult dataclass behavior."""

    def test_default_values(self) -> None:
        r = ContingencyResult(success=True)
        assert r.contingency is True
        assert r.deferred is False
        assert r.content == ""
        assert r.model == ""

    def test_deferred_result(self) -> None:
        r = ContingencyResult(
            success=False, reason="Deferred", deferred=True,
        )
        assert r.deferred
        assert not r.success
