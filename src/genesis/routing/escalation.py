"""Provider failure escalation — creates observations when providers fail persistently.

Subscribes to the event bus for breaker.tripped events. When a provider
trips its circuit breaker N times without recovery, creates a high-priority
observation so the ego picks it up in its next cycle.

The listener MUST be fast (event bus awaits listeners sequentially).
Observation creation is deferred via tracked_task (failures land on the
event bus as task.failed — the reflex nerve — as well as in the log).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime

from genesis.observability.types import GenesisEvent, Severity, Subsystem
from genesis.util.tasks import tracked_task

logger = logging.getLogger(__name__)

# Trip threshold before creating an observation.
# 5 trips ≈ 10 minutes of cycling (120s open duration × 5 cycles).
_TRIP_THRESHOLD = 5

# How long a provider must have been continuously failing before the user is
# told. The escalation observation above fires at ~10 minutes, which is the
# right threshold for a DASHBOARD row and far too eager for a notification —
# most breaker trips resolve themselves well inside an hour, so anything shorter
# pages on noise. An earlier version of this comment cited an
# `_ONGOING_FLOOR_S` constant in the health subsystem as precedent; no such
# symbol exists anywhere in the repo, so the hour stands on the reasoning above
# rather than on a borrowed threshold.
_NOTIFY_AFTER_S = 3600.0


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
        state = self._state.setdefault(
            provider,
            {
                "trip_count": 0,
                "first_trip_at": None,
                "escalated": False,
            },
        )
        state["trip_count"] += 1
        if state["first_trip_at"] is None:
            state["first_trip_at"] = self._clock().isoformat()

        if state["trip_count"] >= _TRIP_THRESHOLD and not state["escalated"]:
            state["escalated"] = True
            # Defer DB write to a tracked background task — don't block emit()
            tracked_task(
                self._create_observation(provider, state),
                name=f"escalation-obs-{provider}",
                event_bus=self._event_bus,
                subsystem=Subsystem.ROUTING,
            )

        # Once escalated, every further trip re-checks whether the outage is now
        # old enough to be worth telling the user about. Re-checking (rather than
        # deciding once at escalation time) is the point: at escalation the
        # outage is ~10 minutes old and below the floor, and post-fix the breaker
        # backs off to a trip every 30min-4h, so this runs a handful of times a
        # day at most. Deferred like the write above — `_on_event` runs inside
        # `emit()` and must stay fast.
        #
        # KNOWN GAP, deliberately not fixed here: `escalated` is in-memory, so a
        # restart mid-outage waits for 5 FRESH trips before re-checking. Dropping
        # this conjunct was tried and REVERTED — it lets a SINGLE trip on a
        # provider with a stranded unresolved row page the user with a multi-day
        # outage claim. Closing it safely needs a startup reconcile that resolves
        # rows for providers whose breakers are not open; see the follow-up.
        if state["escalated"] and not state.get("notified"):
            tracked_task(
                self._maybe_notify(provider),
                name=f"escalation-notify-{provider}",
                event_bus=self._event_bus,
                subsystem=Subsystem.ROUTING,
            )

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
            asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "record_recovery('%s'): no running loop; skipping observation resolve",
                provider,
            )
            return
        tracked_task(
            self._resolve_observation(provider),
            name=f"escalation-resolve-{provider}",
            event_bus=self._event_bus,
            subsystem=Subsystem.ROUTING,
        )

    async def _create_observation(self, provider: str, state: dict) -> None:
        """Create a high-priority observation for a persistently failing provider."""
        from genesis.db.crud import observations

        content = json.dumps(
            {
                "provider": provider,
                "trip_count": state["trip_count"],
                "first_trip_at": state["first_trip_at"],
                "message": (
                    f"Provider '{provider}' has tripped its circuit breaker "
                    f"{state['trip_count']} times since {state['first_trip_at']} "
                    f"without recovery. All calls are falling back to other providers."
                ),
            }
        )
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
                # KNOWN GAP, deliberately not fixed here: this stamps the row
                # when the 5th trip lands, so the duration every consumer reads
                # is short by the escalation ramp (~15-30 min, more under
                # backoff). `first_trip_at` — the true start — is carried in the
                # content JSON below but read by nothing. Backdating this column
                # to it was tried and REVERTED: the anchor is written once per
                # state entry (:72) and cleared only by `record_recovery`, so a
                # flapping provider (success_threshold=2 means one clean call
                # does not clear a trip) keeps a weeks-old anchor and the row is
                # then born already past the 1h notification floor — a critical
                # page with a fabricated multi-day duration. Fixing this needs
                # the anchor bounded first; see the follow-up.
                created_at=self._clock().isoformat(),
                content_hash=content_hash,
                skip_if_duplicate=True,
            )
            if obs_id:
                logger.warning(
                    "Created observation %s for provider '%s' failure (%d trips since %s)",
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

        # BOTH rows: the escalation record and the user-notification record.
        # Leaving the notify row open would make the first outage in a
        # provider's life the only one the user ever hears about, because
        # `skip_if_duplicate` suppresses on an unresolved row of the same hash.
        # NOTIFY FIRST, deliberately. These are two separately committed
        # statements — there is no transaction facility on this connection
        # (`SerializedConnection.__aenter__` is a no-op) — so one can succeed
        # and the next fail. Order decides which row survives that. Notify-row
        # open = `skip_if_duplicate` (which keys on an UNRESOLVED row of the
        # same hash) silences this provider until some later recovery happens
        # to succeed: silent. Failure-row open = the dashboard shows a provider
        # as failing when it is not, and the next recovery clears it: visible.
        # Visible beats silent at identical cost.
        content_hashes = (
            self._notify_content_hash(provider),
            self._provider_content_hash(provider),
        )
        try:
            resolved = 0
            for content_hash in content_hashes:
                resolved += await observations.resolve_by_content_hash(
                    self._db,
                    source="routing",
                    content_hash=content_hash,
                    resolved_at=self._clock().isoformat(),
                    resolution_notes=(
                        f"auto-resolved: provider '{provider}' recovered (circuit breaker closed)"
                    ),
                )
            if resolved:
                logger.info(
                    "Auto-resolved %d provider_failure observation(s) for recovered provider '%s'",
                    resolved,
                    provider,
                )
        except Exception:
            logger.error(
                "Failed to resolve provider_failure observation for '%s'",
                provider,
                exc_info=True,
            )

    def _outage_started_at(self, row: dict) -> datetime | None:
        """Parse an observation's ``created_at`` into an aware UTC datetime.

        Handles both shapes that reach this table: ``create()`` writes an
        ISO string with an offset, while SQLite's ``datetime('now')`` DEFAULT
        writes a naive UTC string. A naive value is therefore UTC, not local.
        """
        raw = row.get("created_at")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    async def _maybe_notify(self, provider: str) -> None:
        """Tell the user ONCE that a provider has been dead long enough to matter.

        Reads the age off the unresolved ``provider_failure`` row rather than
        this object's in-memory state, for two reasons: that row IS the outage
        clock (the same one `_resolve_observation` clears on recovery), and it
        survives a restart, which the in-memory dict does not.

        Delivery reuses the existing fire-once path rather than adding one — a
        ``priority="critical"`` observation is picked up by
        `outreach/scheduler.py::_critical_observations_job`, batched into a
        single message, and `mark_surfaced` guarantees it is never re-sent.
        `skip_if_duplicate` keys on an UNRESOLVED row with the same hash, so a
        second notification cannot be written while the first is still open —
        including from a freshly restarted process.
        """
        from genesis.db.crud import observations

        failure_hash = self._provider_content_hash(provider)
        try:
            rows = await observations.query(
                self._db,
                source="routing",
                type="provider_failure",
                resolved=False,
                limit=100,
            )
        except Exception:
            logger.error(
                "could not read the outage clock for provider '%s'", provider, exc_info=True
            )
            return

        # The outage started at the EARLIEST unresolved record, not an arbitrary
        # one. More than one can exist despite `skip_if_duplicate`: that check
        # matches on (source, content_hash, resolved, origin_class), so a row
        # written through a different origin_class — or present before this
        # provider was first seen by THIS process — does not suppress a second.
        # Taking the newest would reset the outage clock on every duplicate,
        # which is the exact failure this whole change exists to remove.
        starts = [
            s
            for r in rows
            if r.get("content_hash") == failure_hash
            and (s := self._outage_started_at(r)) is not None
        ]
        if not starts:
            # No unresolved failure record → nothing to measure an outage from.
            # Absence of evidence is not an outage; say nothing.
            return
        started = min(starts)
        elapsed = (self._clock() - started).total_seconds()
        if elapsed < _NOTIFY_AFTER_S:
            return

        hours = elapsed / 3600.0
        human = f"{hours / 24:.1f} days" if hours >= 24 else f"{hours:.1f} hours"
        try:
            obs_id = await observations.create(
                self._db,
                id=str(uuid.uuid4()),
                source="routing",
                type="provider_failure",
                content=json.dumps(
                    {
                        "provider": provider,
                        "outage_started_at": started.isoformat(),
                        "message": (
                            f"Provider '{provider}' has been failing every call for "
                            f"{human} and has not recovered. Calls are falling back to "
                            f"other providers in each chain. If this provider is a paid "
                            f"or entitlement-gated model, the account may need attention."
                        ),
                    }
                ),
                priority="critical",
                category="system_health",
                created_at=self._clock().isoformat(),
                content_hash=self._notify_content_hash(provider),
                skip_if_duplicate=True,
            )
        except Exception:
            logger.error(
                "failed to write dead-provider notification for '%s'", provider, exc_info=True
            )
            return

        if obs_id:
            logger.warning(
                "Notified user: provider '%s' has been failing for %s", provider, human
            )
        # Set the in-memory suppressor ONLY on a real write. Setting it on the
        # too-young path would make a provider that is currently 20 minutes down
        # never notify at all, because the outage crosses the floor later and
        # this process would already have stopped checking. The durable dedup is
        # `skip_if_duplicate`; this flag only spares a DB read per trip.
        tracked = self._state.get(provider)
        if tracked is not None and obs_id:
            tracked["notified"] = True

    @staticmethod
    def _notify_content_hash(provider: str) -> str:
        return hashlib.sha256(f"provider_dead_notify:{provider}".encode()).hexdigest()

    @staticmethod
    def _provider_content_hash(provider: str) -> str:
        """Deterministic per-provider hash — MUST match between create + resolve."""
        return hashlib.sha256(f"provider_failure:{provider}".encode()).hexdigest()
