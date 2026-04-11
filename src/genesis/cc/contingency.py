"""CC Contingency Dispatcher — routes CC work through API when CC is unavailable.

When the CC Max subscription hits usage limits, this dispatcher routes
eligible work through the API router using contingency call sites defined
in model_routing.yaml. Deep, Light, and Strategic reflections are deferred
to the work queue — quality reflection requires Claude. Only Micro
reflections and foreground conversation use contingency providers.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from genesis.awareness.types import Depth

if TYPE_CHECKING:
    from genesis.resilience.deferred_work import DeferredWorkQueue
    from genesis.routing.model_profiles import ModelProfileRegistry
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

# How long deferred items persist before expiry. Matches typical CC Max rate
# limit reset window. Referenced from awareness/loop.py and reflection_bridge.py.
RATE_LIMIT_DEFERRAL_TTL_S = 14400  # 4 hours

# Depths that should be deferred (not routed to API) when CC is down.
# Deep and Light reflections require Claude-quality reasoning — falling back
# to inferior models defeats the purpose. Wait for CC to come back instead.
_DEFER_DEPTHS = frozenset({Depth.STRATEGIC, Depth.DEEP, Depth.LIGHT})

# Call site mapping for contingency routing (only Micro reaches this path)
_CONTINGENCY_CALL_SITES = {
    Depth.MICRO: "contingency_micro",  # Micro uses free models — never expensive
}


class ContingencyResult:
    """Result from a contingency dispatch."""

    __slots__ = ("success", "content", "model", "contingency", "reason", "deferred")

    def __init__(
        self,
        *,
        success: bool,
        content: str = "",
        model: str = "",
        contingency: bool = True,
        reason: str = "",
        deferred: bool = False,
    ):
        self.success = success
        self.content = content
        self.model = model
        self.contingency = contingency
        self.reason = reason
        self.deferred = deferred


class CCContingencyDispatcher:
    """Routes CC work through API when CC is unavailable."""

    def __init__(
        self,
        *,
        router: Router,
        profile_registry: ModelProfileRegistry | None = None,
        deferred_queue: DeferredWorkQueue | None = None,
    ):
        self._router = router
        self._profiles = profile_registry
        self._deferred = deferred_queue

    async def dispatch_reflection(
        self,
        depth: Depth,
        prompt: str,
        system_prompt: str,
    ) -> ContingencyResult:
        """Route a reflection through API instead of CC.

        Deep, Light, and Strategic reflections are deferred to the work queue.
        Only Micro reflections route through contingency call sites.
        """
        if depth in _DEFER_DEPTHS:
            if self._deferred:
                await self._deferred.enqueue(
                    work_type=f"reflection_{depth.value.lower()}",
                    call_site_id=None,
                    priority=30,
                    payload=json.dumps({
                        "depth": depth.value,
                        "reason": "CC unavailable — deferred",
                    }),
                    reason="CC unavailable — deferred until available",
                    staleness_policy="ttl",
                    staleness_ttl_s=RATE_LIMIT_DEFERRAL_TTL_S,
                )
            return ContingencyResult(
                success=False,
                reason=f"{depth.value} reflection deferred — CC unavailable",
                deferred=True,
            )

        call_site = _CONTINGENCY_CALL_SITES.get(depth)
        if call_site is None:
            logger.warning("Unexpected depth %s in contingency — falling back to deep chain", depth)
            call_site = "5_deep_reflection"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        try:
            result = await self._router.route_call(call_site, messages)
        except Exception:
            logger.exception("Contingency routing failed for %s reflection", depth.value)
            return ContingencyResult(
                success=False,
                reason=f"Contingency routing failed for {depth.value}",
            )

        if result.success:
            return ContingencyResult(
                success=True,
                content=result.content or "",
                model=result.provider_used or "",
            )
        return ContingencyResult(
            success=False,
            reason=f"Contingency failed: {result.error}",
        )

    async def dispatch_conversation(
        self,
        messages: list[dict],
        system_prompt: str,
    ) -> ContingencyResult:
        """Route foreground conversation through API.

        Operates in degraded-but-alive mode: same system prompt, conversation
        history maintained, but no CC tool access.
        """
        full_messages = [{"role": "system", "content": system_prompt}] + messages

        try:
            result = await self._router.route_call(
                "contingency_foreground", full_messages,
            )
        except Exception:
            logger.exception("Contingency foreground routing failed")
            return ContingencyResult(
                success=False,
                reason="Contingency foreground routing failed",
            )

        if result.success:
            return ContingencyResult(
                success=True,
                content=result.content or "",
                model=result.provider_used or "",
            )
        return ContingencyResult(
            success=False,
            reason=f"Contingency foreground failed: {result.error}",
        )

