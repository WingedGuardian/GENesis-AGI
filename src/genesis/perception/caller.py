"""LLMCaller — routes LLM calls through genesis.routing."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from genesis.perception.types import LLMResponse

if TYPE_CHECKING:
    from genesis.observability.events import GenesisEventBus
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)


class LLMCaller:
    """Wraps Router.route_call() for the perception pipeline.

    Converts RoutingResult into LLMResponse. Does NOT record cost
    (Router handles that internally).
    """

    def __init__(
        self,
        *,
        router: Router,
        event_bus: GenesisEventBus | None = None,
    ) -> None:
        self._router = router
        self._event_bus = event_bus

    async def call(
        self,
        prompt: str,
        *,
        call_site_id: str,
    ) -> LLMResponse | None:
        """Send prompt through the router. Returns None if all providers fail."""
        messages = [{"role": "user", "content": prompt}]
        result = await self._router.route_call(call_site_id, messages)

        if not result.success:
            logger.warning(
                "LLM call failed for %s: %s",
                call_site_id,
                getattr(result, "error", "unknown"),
            )
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem

                await self._event_bus.emit(
                    Subsystem.PERCEPTION,
                    Severity.WARNING,
                    "reflection.call_failed",
                    f"LLM call failed for {call_site_id}",
                    call_site_id=call_site_id,
                )
            return None

        return LLMResponse(
            text=result.content,
            model=result.provider_used or "unknown",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            latency_ms=getattr(result, "latency_ms", 0),
        )
