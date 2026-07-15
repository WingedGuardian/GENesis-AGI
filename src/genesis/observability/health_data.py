"""HealthDataService — unified health snapshot for dashboard and health MCP tools.

The snapshot() method delegates to individual functions in observability/snapshots/
for cleaner organization. Each function takes explicit dependencies as parameters.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
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
from genesis.observability.probe_transitions import ProbeTransition, ProbeTransitionTracker
from genesis.observability.types import Severity, Subsystem

logger = logging.getLogger(__name__)

# Backward-compat constant still imported by reflection/context code.
CC_JSONL_DIR = str(Path.home() / ".claude" / "projects" / cc_project_dir())

# Infra probes carrying a stable string ``status`` field worth tracking for
# healthy<->unhealthy transitions. Deliberately EXCLUDES: cpu (utilization noise
# — a flip at 95% would spam the feed), disk (no ``status`` on success), cc_tmp
# (tier-nested, no top-level status), cc_slots (a list), and the conditional
# ambient/ollama probes. Excluded on purpose, not silently dropped.
_TRACKED_PROBES: tuple[str, ...] = (
    "genesis.db",
    "qdrant",
    "guardian",
    "qdrant_collections",
    "scheduler",
    "container_memory",
)


def _or_error(result: object) -> object:
    """Isolate a failed gather section.

    With ``return_exceptions=True`` a raising sub-snapshot lands in the results
    list as an exception instead of aborting the whole snapshot. Convert a normal
    ``Exception`` into a ``{"status": "error"}`` section (every consumer reads
    sections defensively via ``.get()``), but re-raise a ``BaseException`` such as
    ``CancelledError`` so cooperative cancellation is never swallowed. Applied
    uniformly to every result so no un-serializable exception object leaks into
    the snapshot dict.
    """
    if isinstance(result, Exception):
        logger.warning("Health snapshot section failed", exc_info=result)
        return {"status": "error", "error": str(result)}
    if isinstance(result, BaseException):
        raise result
    return result


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
        # One tracker per service instance. This service is a singleton in the
        # server process (constructed only in runtime/init/health_data.py), so the
        # tracker sees every snapshot and is the sole emitter of probe transitions.
        # It detects healthy<->unhealthy boundary crossings; it NEVER touches
        # routing state (that is resilience.state's job, kept strictly separate).
        self._probe_tracker = ProbeTransitionTracker()
        # Single-flight: concurrent snapshot() callers coalesce onto one compute.
        self._inflight: asyncio.Task | None = None

    async def snapshot(self) -> dict:
        """Return full system health state, coalescing concurrent callers.

        The snapshot is expensive (systemd subprocesses + DB + network). Callers
        that overlap — the two ego-context builders on an awareness tick, the
        sentinel dispatcher, the morning report — share ONE in-flight computation
        instead of each triggering a full recompute. The returned dict is
        READ-ONLY by contract (every caller reads via ``.get()``), so it is shared
        among coalesced callers without a defensive copy.

        The shared task is awaited through ``asyncio.shield`` so one caller's
        cancellation — e.g. the dashboard route's 15s ``_async_route`` timeout —
        cannot cancel the in-flight snapshot out from under the other coalesced
        callers (who catch ``Exception``, not ``CancelledError``). ``_inflight``
        is released by the task's own done-callback, not a caller's ``finally``,
        so it survives the cancelling caller unwinding first; the ``.done()`` guard
        makes correctness independent of when that callback runs.
        """
        inflight = self._inflight
        if inflight is not None and not inflight.done():
            return await asyncio.shield(inflight)
        # No await between the done() check and the assignment below → race-free
        # on the single-threaded loop.
        task = asyncio.create_task(self._compute_snapshot())
        self._inflight = task
        task.add_done_callback(self._release_inflight)
        return await asyncio.shield(task)

    def _release_inflight(self, task: asyncio.Task) -> None:
        """Done-callback: drop the in-flight handle when the compute finishes.

        Runs on the loop (scheduled via ``call_soon``). Clears only if the handle
        is still ours, and retrieves any exception so a fully-orphaned failed
        compute (every awaiter cancelled before completion) does not emit a
        "Task exception was never retrieved" warning.
        """
        if self._inflight is task:
            self._inflight = None
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.debug("Orphaned health snapshot compute failed", exc_info=exc)

    async def _recent_provider_fallbacks(self) -> dict:
        """Per-provider fallback counts (last 24h) for the API-keys card.

        Sources ``provider.fallback`` events — the only place a skipped/failed
        provider's identity is recorded (the activity tracker never sees
        missing-key / circuit-breaker skips, which bail before ``.record()``).
        Self-contained error handling returns ``{}`` on any failure so a bad
        query can never poison the ``api_keys`` section (its result is consumed
        synchronously outside the gather's ``_or_error`` isolation).
        """
        if self._db is None:
            return {}
        try:
            from genesis.db.crud import events as events_crud

            since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
            return await events_crud.recent_provider_fallback_counts(
                self._db, since=since,
            )
        except Exception:
            logger.debug("recent provider-fallback counts query failed", exc_info=True)
            return {}

    async def _compute_snapshot(self) -> dict:
        """Build the full system health state as a dict (uncoalesced)."""
        from genesis.observability.snapshots import (
            api_key_health,
            awareness,
            call_sites,
            cc_sessions,
            conversation_activity,
            cost,
            deploy_health,
            eval_staleness,
            infrastructure,
            mcp_status,
            memory_health,
            outreach_stats,
            proactive_memory_metrics,
            provider_activity,
            queues,
            services_async,
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
        results = await asyncio.gather(
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
            # Offloaded so NO blocking I/O runs on the event loop. services_async
            # threads only the systemctl subprocesses (the sentinel read stays
            # on-loop, atomic); the two file-reading snapshots go to a worker
            # thread whole. Concurrent with the DB/network calls above → ≈0 added
            # wall-clock vs. the old serial post-gather calls.
            services_async(),
            asyncio.to_thread(conversation_activity),
            asyncio.to_thread(proactive_memory_metrics),
            # APPENDED last so the 16 existing positions are untouched (see unpack).
            self._recent_provider_fallbacks(),
            # Deploy staleness — merged-vs-deployed drift (update.sh age, commits
            # behind, missing units, host guardian). Does its own to_thread.
            deploy_health(self._db),
            return_exceptions=True,
        )
        # Isolate any failed section uniformly: one raising sub-snapshot degrades
        # to {"status": "error"} instead of taking down the whole snapshot.
        results = [_or_error(r) for r in results]
        (
            r_call_sites, r_cc_sessions, r_infrastructure, r_queues, r_surplus,
            r_cost, r_awareness, r_outreach, r_mcp, r_provider_activity,
            r_memory_health, r_eval_staleness, r_vcr,
            r_services, r_conversation, r_proactive, r_provider_fallbacks,
            r_deploy_health,
        ) = results

        # Surface infra probe healthy<->unhealthy transitions to the activity
        # feed. Best-effort: a bad emit must never poison the shared snapshot.
        try:
            await self._emit_probe_transitions(r_infrastructure)
        except Exception:
            logger.debug("Probe-transition emit pass failed", exc_info=True)

        # Fallback counts feed the API-keys card. _recent_provider_fallbacks
        # self-isolates its errors to {}, so this is always a provider map; the
        # isinstance keeps the attach loop safe if that contract ever changes.
        recent_fallbacks = (
            r_provider_fallbacks if isinstance(r_provider_fallbacks, dict) else {}
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
            "services": r_services,
            "api_keys": api_key_health(
                self._routing_config, breakers=self._breakers,
                recent_fallbacks=recent_fallbacks,
            ),
            "mcp_servers": r_mcp,
            "conversation": r_conversation,
            "provider_activity": r_provider_activity,
            "proactive_memory": r_proactive,
            "memory_health": r_memory_health,
            "provider_health": self._serialize_provider_health(),
            "eval_staleness": r_eval_staleness,
            "deploy_health": r_deploy_health,
            "vcr": r_vcr,
        }

    async def _emit_probe_transitions(self, infra: object) -> None:
        """Drive the probe-transition tracker from the freshly-built infra dict,
        emitting one activity event per healthy<->unhealthy crossing.

        Storm guard: if the whole infrastructure section failed (not a dict, or a
        top-level ``status == "error"`` injected by ``_or_error``), skip the cycle
        entirely — otherwise every tracked probe would read "down" at once and
        flood the feed with a false N-probe outage on one transient snapshot error.
        """
        if self._event_bus is None:
            return
        if not isinstance(infra, dict) or infra.get("status") == "error":
            return
        for probe_id in _TRACKED_PROBES:
            entry = infra.get(probe_id)
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            if not status:
                continue
            try:
                transition = self._probe_tracker.observe(probe_id, status)
            except Exception:
                logger.debug("Probe-transition observe failed for %s", probe_id, exc_info=True)
                continue
            if transition is not None:
                await self._emit_transition_event(transition)

    async def _emit_transition_event(self, t: ProbeTransition) -> None:
        """Emit a single ``probe_transition`` activity event (best-effort).

        Recovery (→ healthy) is INFO; a hard down/error is ERROR; a softer
        unhealthy state (e.g. degraded) is WARNING.
        """
        if t.new_class == "healthy":
            severity = Severity.INFO
        elif t.new_status in ("down", "error"):
            severity = Severity.ERROR
        else:
            severity = Severity.WARNING
        message = f"{t.probe_id}: {t.old_status} → {t.new_status}"
        if t.flapping:
            message += " (flapping)"
        try:
            # "from" is a keyword — details MUST be passed via dict-unpack.
            await self._event_bus.emit(
                Subsystem.HEALTH,
                severity,
                "probe_transition",
                message,
                **{
                    "probe": t.probe_id,
                    "from": t.old_status,
                    "to": t.new_status,
                    "from_class": t.old_class,
                    "to_class": t.new_class,
                    "flapping": t.flapping,
                },
            )
        except Exception:
            logger.debug("Probe-transition emit failed for %s", t.probe_id, exc_info=True)

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
