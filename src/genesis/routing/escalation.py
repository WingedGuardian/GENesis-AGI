"""Provider failure escalation — creates observations when providers fail persistently.

Subscribes to the event bus for breaker.tripped events. When a provider
trips its circuit breaker N times without recovery, creates a high-priority
observation so the ego picks it up in its next cycle.

The listener MUST be fast (event bus awaits listeners sequentially).
Observation creation is deferred via asyncio.create_task().
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime

from genesis.observability.types import GenesisEvent, Severity

logger = logging.getLogger(__name__)

# Trip threshold before creating an observation.
# 5 trips ≈ 10 minutes of cycling (120s open duration × 5 cycles).
_TRIP_THRESHOLD = 5


class ProviderEscalation:
    """Track per-provider failures and escalate to observations."""

    def __init__(self, db, event_bus, *, clock=None):
        self._db = db
        self._event_bus = event_bus
        self._clock = clock or (lambda: datetime.now(UTC))
        # Per-provider tracking state:
        # {name: {"trip_count": int, "first_trip_at": str, "escalated": bool}}
        self._state: dict[str, dict] = {}

    def attach(self) -> None:
        """Subscribe to routing events on the event bus."""
        self._event_bus.subscribe(self._on_event, min_severity=Severity.WARNING)
        logger.info("Provider escalation listener attached to event bus")

    async def _on_event(self, event: GenesisEvent) -> None:
        """Handle breaker.tripped events. Must be fast — runs in emit() path."""
        if event.event_type != "breaker.tripped":
            return

        provider = event.details.get("provider", "unknown")
        state = self._state.setdefault(provider, {
            "trip_count": 0,
            "first_trip_at": None,
            "escalated": False,
        })
        state["trip_count"] += 1
        if state["first_trip_at"] is None:
            state["first_trip_at"] = self._clock().isoformat()

        if state["trip_count"] >= _TRIP_THRESHOLD and not state["escalated"]:
            state["escalated"] = True
            # Defer DB write to a background task — don't block emit()
            task = asyncio.create_task(
                self._create_observation(provider, state),
                name=f"escalation-obs-{provider}",
            )
            task.add_done_callback(self._on_task_done)

    def record_recovery(self, provider: str) -> None:
        """Called when a provider recovers (breaker → CLOSED).

        Clears in-memory tracking AND resolves the provider's lingering
        ``provider_failure`` observation. Without the resolve the pipeline is
        write-only on recovery: the row created on trip survives until its TTL
        and keeps reporting the provider as failing after it has recovered.
        The resolve is unconditional (not gated on in-memory state) so a row
        created before a restart still clears — mirrors the dead-letter
        resolve-on-drain pattern.
        """
        if provider in self._state:
            logger.info(
                "Provider '%s' recovered after %d trips — clearing escalation state",
                provider,
                self._state[provider].get("trip_count", 0),
            )
            del self._state[provider]

        # record_recovery() is called from the SYNC CircuitBreaker.record_success()
        # path. In production that runs inside the async routing call (a loop is
        # present); a sync caller (e.g. a unit test) may have none — guard so we
        # never raise there, and defer the DB resolve like _create_observation does.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "record_recovery('%s'): no running loop; skipping observation resolve",
                provider,
            )
            return
        task = loop.create_task(
            self._resolve_observation(provider),
            name=f"escalation-resolve-{provider}",
        )
        task.add_done_callback(self._on_task_done)

    async def _create_observation(self, provider: str, state: dict) -> None:
        """Create a high-priority observation for a persistently failing provider."""
        from genesis.db.crud import observations

        content = json.dumps({
            "provider": provider,
            "trip_count": state["trip_count"],
            "first_trip_at": state["first_trip_at"],
            "message": (
                f"Provider '{provider}' has tripped its circuit breaker "
                f"{state['trip_count']} times since {state['first_trip_at']} "
                f"without recovery. All calls are falling back to other providers."
            ),
        })
        # Hash on provider name — one unresolved observation per provider.
        # Shared helper so the resolve-on-recovery path computes the SAME hash.
        content_hash = self._provider_content_hash(provider)

        try:
            obs_id = await observations.create(
                self._db,
                id=str(uuid.uuid4()),
                source="routing",
                type="provider_failure",
                content=content,
                priority="high",
                category="system_health",
                created_at=self._clock().isoformat(),
                content_hash=content_hash,
                skip_if_duplicate=True,
            )
            if obs_id:
                logger.warning(
                    "Created observation %s for provider '%s' failure "
                    "(%d trips since %s)",
                    obs_id,
                    provider,
                    state["trip_count"],
                    state["first_trip_at"],
                )
            else:
                logger.debug(
                    "Skipped duplicate observation for provider '%s'",
                    provider,
                )
        except Exception:
            logger.error(
                "Failed to create observation for provider '%s'",
                provider,
                exc_info=True,
            )

    async def _resolve_observation(self, provider: str) -> None:
        """Resolve the lingering provider_failure observation on recovery.

        Keyed on the deterministic per-provider content_hash, so only THIS
        provider's row resolves — a different, still-down provider's row is
        untouched. Idempotent (no-op when nothing matches) and non-fatal.
        """
        from genesis.db.crud import observations

        content_hash = self._provider_content_hash(provider)
        try:
            resolved = await observations.resolve_by_content_hash(
                self._db,
                source="routing",
                content_hash=content_hash,
                resolved_at=self._clock().isoformat(),
                resolution_notes=(
                    f"auto-resolved: provider '{provider}' recovered "
                    f"(circuit breaker closed)"
                ),
            )
            if resolved:
                logger.info(
                    "Auto-resolved %d provider_failure observation(s) for "
                    "recovered provider '%s'",
                    resolved,
                    provider,
                )
        except Exception:
            logger.error(
                "Failed to resolve provider_failure observation for '%s'",
                provider,
                exc_info=True,
            )

    @staticmethod
    def _provider_content_hash(provider: str) -> str:
        """Deterministic per-provider hash — MUST match between create + resolve."""
        return hashlib.sha256(f"provider_failure:{provider}".encode()).hexdigest()

    @staticmethod
    def _on_task_done(task: asyncio.Task) -> None:
        """Log exceptions from background observation tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(
                "Escalation observation task failed: %s",
                exc,
                exc_info=exc,
            )
