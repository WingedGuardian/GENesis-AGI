"""HealthDataService — unified health snapshot for dashboard and health MCP tools.

The snapshot() method delegates to individual functions in observability/snapshots/
for cleaner organization. Each function takes explicit dependencies as parameters.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from genesis.resilience.cc_budget import CCBudgetTracker
    from genesis.resilience.deferred_work import DeferredWorkQueue
    from genesis.resilience.state import ResilienceStateMachine
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry
    from genesis.routing.cost_tracker import CostTracker
    from genesis.routing.dead_letter import DeadLetterQueue
    from genesis.routing.types import RoutingConfig
    from genesis.surplus.scheduler import SurplusScheduler

from genesis.env import cc_project_dir

logger = logging.getLogger(__name__)

# Backward-compat constant still imported by reflection/context code.
CC_JSONL_DIR = str(Path.home() / ".claude" / "projects" / cc_project_dir())


class HealthDataService:
    """Aggregates all health sources into a single snapshot dict.

    All constructor dependencies are optional — if a subsystem isn't available,
    its section returns "unknown" status.
    """

    def __init__(
        self,
        *,
        circuit_breakers: CircuitBreakerRegistry | None = None,
        routing_config: RoutingConfig | None = None,
        cost_tracker: CostTracker | None = None,
        cc_budget: CCBudgetTracker | None = None,
        deferred_queue: DeferredWorkQueue | None = None,
        dead_letter: DeadLetterQueue | None = None,
        db: aiosqlite.Connection | None = None,
        surplus_scheduler: SurplusScheduler | None = None,
        learning_scheduler: object | None = None,
        resilience_state_machine: ResilienceStateMachine | None = None,
        activity_tracker: object | None = None,
    ) -> None:
        self._breakers = circuit_breakers
        self._routing_config = routing_config
        self._cost_tracker = cost_tracker
        self._cc_budget = cc_budget
        self._deferred_queue = deferred_queue
        self._dead_letter = dead_letter
        self._db = db
        self._surplus = surplus_scheduler
        self._learning_scheduler = learning_scheduler
        self._state_machine = resilience_state_machine
        self._activity_tracker = activity_tracker

    async def snapshot(self) -> dict:
        """Return full system health state as a dict."""
        from genesis.observability.snapshots import (
            api_key_health,
            awareness,
            call_sites,
            cc_sessions,
            conversation_activity,
            cost,
            infrastructure,
            mcp_status,
            outreach_stats,
            proactive_memory_metrics,
            provider_activity,
            queues,
            services,
            surplus_status,
        )

        now = datetime.now(UTC).isoformat()
        return {
            "timestamp": now,
            "call_sites": await call_sites(self._db, self._routing_config, self._breakers),
            "cc_sessions": await cc_sessions(self._db, self._cc_budget, self._state_machine),
            "resilience": self._resilience_state(),
            "infrastructure": await infrastructure(
                self._db, self._routing_config, self._learning_scheduler, self._state_machine
            ),
            "queues": await queues(self._db, self._deferred_queue, self._dead_letter),
            "surplus": await surplus_status(self._db, self._surplus),
            "cost": await cost(self._db, self._cost_tracker, self._cc_budget),
            "awareness": await awareness(self._db),
            "outreach_stats": await outreach_stats(self._db),
            "services": services(),
            "api_keys": api_key_health(self._routing_config),
            "mcp_servers": await mcp_status(),
            "conversation": conversation_activity(),
            "provider_activity": await provider_activity(self._activity_tracker),
            "proactive_memory": proactive_memory_metrics(),
        }

    def _resilience_state(self) -> str:
        """Compute resilience state from circuit breaker registry."""
        from genesis.observability.snapshots.infrastructure import resilience_state

        return resilience_state(self._breakers, self._state_machine)

    async def validate_api_keys(self) -> None:
        """Test each provider's API key with a lightweight call. Cache results."""
        from genesis.observability.snapshots.api_keys import validate_api_keys

        await validate_api_keys(self._routing_config)
