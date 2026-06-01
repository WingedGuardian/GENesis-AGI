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
        """Called when a provider recovers (breaker → CLOSED). Reset state."""
        if provider in self._state:
            logger.info(
                "Provider '%s' recovered after %d trips — clearing escalation state",
                provider,
                self._state[provider].get("trip_count", 0),
            )
            del self._state[provider]

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
        # Hash on provider name — one unresolved observation per provider
        content_hash = hashlib.sha256(
            f"provider_failure:{provider}".encode(),
        ).hexdigest()

        try:
            obs_id = await observations.create(
                self._db,
                id=str(uuid.uuid4()),
                source="routing",
                type="provider_failure",
                content=content,
                priority="high",
                category="system_health",
                created_at=self._clock(),
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
