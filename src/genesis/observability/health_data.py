"""HealthDataService — unified health snapshot for dashboard and health MCP tools.

The snapshot() method delegates to individual functions in observability/snapshots/
for cleaner organization. Each function takes explicit dependencies as parameters.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from genesis.observability.events import GenesisEventBus
    from genesis.observability.provider_health import ProviderHealthChecker
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
        provider_health_checker: ProviderHealthChecker | None = None,
        event_bus: GenesisEventBus | None = None,
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
        self._provider_health = provider_health_checker
        self._event_bus = event_bus

    async def snapshot(self) -> dict:
        """Return full system health state as a dict."""
        from genesis.observability.snapshots import (
            api_key_health,
            awareness,
            call_sites,
            cc_sessions,
            conversation_activity,
            cost,
            eval_staleness,
            infrastructure,
            mcp_status,
            memory_health,
            outreach_stats,
            proactive_memory_metrics,
            provider_activity,
            queues,
            services,
            surplus_status,
        )

        now = datetime.now(UTC).isoformat()

        # Probe providers if cache is stale (probes are free — /v1/models endpoints)
        probe_results = None
        if self._provider_health:
            if self._provider_health.is_stale():
                try:
                    await self._provider_health.probe_all()
                except Exception:
                    logger.warning("Provider health probe failed", exc_info=True)
            probe_results = self._provider_health.results

        # Run the independent async sub-snapshots CONCURRENTLY. Serial execution
        # summed to ~9s (and ballooned to 20s+ under load), hanging the dashboard
        # and the Guardian's health probe. gather makes the total ≈ max(sub-call)
        # instead of the sum: aiosqlite serializes DB queries on the single
        # connection (safe), while network-bound calls (memory_health/Qdrant,
        # mcp_status) overlap with them and each other.
        (
            r_call_sites, r_cc_sessions, r_infrastructure, r_queues, r_surplus,
            r_cost, r_awareness, r_outreach, r_mcp, r_provider_activity,
            r_memory_health, r_eval_staleness, r_vcr,
        ) = await asyncio.gather(
            call_sites(
                self._db, self._routing_config, self._breakers,
                probe_results=probe_results, state_machine=self._state_machine,
            ),
            cc_sessions(self._db, self._cc_budget, self._state_machine),
            infrastructure(
                self._db, self._routing_config, self._learning_scheduler, self._state_machine
            ),
            queues(self._db, self._deferred_queue, self._dead_letter, self._event_bus),
            surplus_status(self._db, self._surplus),
            cost(self._db, self._cost_tracker, self._cc_budget),
            awareness(self._db),
            outreach_stats(self._db),
            mcp_status(),
            provider_activity(self._activity_tracker),
            memory_health(self._db),
            eval_staleness(self._db),
            self._vcr_snapshot(),
        )

        return {
            "timestamp": now,
            "call_sites": r_call_sites,
            "cc_sessions": r_cc_sessions,
            "resilience": self._resilience_state(),
            "infrastructure": r_infrastructure,
            "queues": r_queues,
            "surplus": r_surplus,
            "cost": r_cost,
            "awareness": r_awareness,
            "outreach_stats": r_outreach,
            "services": services(),
            "api_keys": api_key_health(self._routing_config, breakers=self._breakers),
            "mcp_servers": r_mcp,
            "conversation": conversation_activity(),
            "provider_activity": r_provider_activity,
            "proactive_memory": proactive_memory_metrics(),
            "memory_health": r_memory_health,
            "provider_health": self._serialize_provider_health(),
            "eval_staleness": r_eval_staleness,
            "vcr": r_vcr,
        }

    async def _vcr_snapshot(self) -> dict:
        """Verified Completion Rate for ego proposals."""
        if self._db is None:
            return {}
        try:
            from genesis.db.crud.ego import compute_vcr
            return await compute_vcr(self._db, days=30)
        except Exception:
            logger.debug("VCR snapshot failed", exc_info=True)
            return {}

    def _serialize_provider_health(self) -> dict:
        """Serialize provider probe results for the snapshot."""
        if not self._provider_health:
            return {}
        return {
            name: {
                "reachable": r.reachable,
                "model_available": r.model_available,
                "latency_ms": r.latency_ms,
                "error": r.error,
                "checked_at": r.checked_at,
            }
            for name, r in self._provider_health.results.items()
        }

    def _resilience_state(self) -> dict:
        """Compute resilience state with detail from circuit breaker registry."""
        from genesis.observability.snapshots.infrastructure import resilience_state_detail

        return resilience_state_detail(self._breakers, self._state_machine)

    async def validate_api_keys(self) -> None:
        """Test each provider's API key with a lightweight call. Cache results."""
        from genesis.observability.snapshots.api_keys import validate_api_keys

        await validate_api_keys(self._routing_config)
