"""Dead-letter queue for failed routing operations."""

from __future__ import annotations

import ast
import json
import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.db.crud import dead_letter as dl_crud
from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem

# Sentinel status-reason suffix used when the orphan scan marks an item
# expired because its target_provider was dropped from config on reload.
# Kept distinct from the 72h age-based expiry reason so operators can tell
# the two apart in logs and audits.
ORPHAN_REASON_PROVIDER_REMOVED = "provider_removed_on_reload"

logger = logging.getLogger(__name__)


def _is_unknown_call_site_error(error: str | None) -> bool:
    """Return True when a RoutingResult.error signals an unknown call_site_id.

    The router returns this error when ``route_call()`` is invoked with a
    call_site_id that is not present in the current routing config. We use
    it in redispatch() to expire stale DLQ items whose call_site_id was
    renamed or removed (e.g., contingency_inbox → contingency_micro after
    a config reload), instead of retrying them forever until expire_old()
    cleans them up 72h later.

    The sentinel string lives on :mod:`genesis.routing.router` as
    ``UNKNOWN_CALL_SITE_ERROR_PREFIX``. Import locally to avoid a hard
    import cycle (router already imports DeadLetterQueue).
    """
    if not error:
        return False
    from genesis.routing.router import UNKNOWN_CALL_SITE_ERROR_PREFIX
    return error.startswith(UNKNOWN_CALL_SITE_ERROR_PREFIX)


class DeadLetterQueue:
    def __init__(self, db: aiosqlite.Connection, *, clock=None, event_bus: GenesisEventBus | None = None):
        self.db = db
        self._clock = clock or (lambda: datetime.now(UTC))
        self._event_bus = event_bus

    async def enqueue(
        self,
        operation_type: str,
        payload: str | dict,
        target_provider: str,
        failure_reason: str,
    ) -> str:
        """Add a failed operation to the dead-letter queue. Returns the ID."""
        item_id = str(uuid.uuid4())
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        try:
            await dl_crud.create(
                self.db,
                id=item_id,
                operation_type=operation_type,
                payload=payload,
                target_provider=target_provider,
                failure_reason=failure_reason,
                created_at=self._clock().isoformat(),
            )
        except Exception:
            logger.error(
                "Dead letter enqueue FAILED — operation lost: type=%s provider=%s reason=%s",
                operation_type, target_provider, failure_reason, exc_info=True,
            )
            raise
        if self._event_bus:
            await self._event_bus.emit(
                Subsystem.ROUTING, Severity.WARNING,
                "dead_letter.enqueued",
                f"Dead-lettered {operation_type} for {target_provider}: {failure_reason}",
                item_id=item_id, provider=target_provider,
                operation=operation_type, reason=failure_reason,
            )
        return item_id

    async def replay_pending(self, target_provider: str) -> int:
        """Mark all pending items for a provider as 'replayed'. Returns count."""
        items = await dl_crud.query_pending(self.db, target_provider=target_provider)
        for item in items:
            await dl_crud.update_status(self.db, item["id"], status="replayed")
        return len(items)

    async def scan_orphans_by_provider(
        self, active_providers: Iterable[str],
    ) -> int:
        """Expire pending items whose target_provider is no longer in config.

        Proactive complement to ``redispatch()``'s reactive call_site_id
        orphan cleanup. When routing config is hot-reloaded and a provider
        is removed (contract ended, key rotated out, etc.), every pending
        DLQ item targeting that provider will fail redispatch forever
        until the 72h age-based ``expire_old`` sweep finally catches them.
        This method short-circuits that wait: on every config reload, run
        a single atomic SQL UPDATE that marks every pending item whose
        ``target_provider`` is not in the active provider set as expired
        in one round-trip, and returns the affected rows via ``RETURNING``.

        The SQL-side filter is deliberate: paginating via ``query_pending``
        would cap the scan at 50 items per call, silently leaving larger
        orphan batches to wait 72h for ``expire_old``. That defeats the
        whole purpose of the proactive feature.

        Args:
            active_providers: iterable of provider names currently in config.

        Returns:
            Count of orphans expired on this scan.
        """
        # Materialize once — used only for the SQL IN-list.
        active = list(set(active_providers))
        expired = await dl_crud.expire_orphans_by_provider(
            self.db, active_providers=active,
        )
        for item_id, target in expired:
            logger.info(
                "Expired dead letter %s: target_provider %r no longer "
                "in active config (%s)",
                item_id, target, ORPHAN_REASON_PROVIDER_REMOVED,
            )
        if expired and self._event_bus:
            await self._event_bus.emit(
                Subsystem.ROUTING, Severity.INFO,
                "dead_letter.orphans_expired",
                f"Expired {len(expired)} DLQ orphan(s) after config reload",
                count=len(expired), reason=ORPHAN_REASON_PROVIDER_REMOVED,
                orphan_ids=[item_id for item_id, _ in expired],
            )
        return len(expired)

    async def expire_old(self, max_age_hours: int = 72) -> int:
        """Mark pending items older than max_age_hours as 'expired'. Returns count."""
        cutoff = self._clock() - timedelta(hours=max_age_hours)
        cutoff_iso = cutoff.isoformat()
        items = await dl_crud.query_pending(self.db)
        count = 0
        for item in items:
            if item["created_at"] < cutoff_iso:
                await dl_crud.update_status(self.db, item["id"], status="expired")
                count += 1
        return count

    async def redispatch(self, dispatch_fn) -> tuple[int, int]:
        """Re-dispatch pending items via dispatch_fn(call_site_id, messages).

        Items with parseable call_site_id are re-dispatched. Legacy items
        without call_site_id are marked 'expired' (can't re-dispatch).

        Returns (succeeded, failed).
        """
        items = await dl_crud.query_pending(self.db)
        succeeded = failed = 0
        for item in items:
            try:
                payload = json.loads(item["payload"])
            except (json.JSONDecodeError, TypeError):
                # Corrupt or non-JSON payload — can't re-dispatch
                await dl_crud.update_status(self.db, item["id"], status="expired")
                logger.info("Expired dead letter %s: unparseable payload", item["id"])
                continue

            call_site_id = payload.get("call_site_id")
            if not call_site_id:
                # Legacy item with truncated payload — can't re-dispatch
                await dl_crud.update_status(self.db, item["id"], status="expired")
                logger.info("Expired dead letter %s: no call_site_id (legacy)", item["id"])
                continue

            raw_messages = payload.get("messages", [])
            messages: list[dict] = []
            for m in raw_messages:
                if isinstance(m, dict):
                    messages.append(m)
                elif isinstance(m, str):
                    # Legacy entries stored via str(dict) — try to recover
                    try:
                        messages.append(json.loads(m))
                    except json.JSONDecodeError:
                        try:
                            parsed = ast.literal_eval(m)
                            if isinstance(parsed, dict):
                                messages.append(parsed)
                        except (ValueError, SyntaxError):
                            logger.warning(
                                "Dropped unparseable legacy message in dead letter: %.200r", m,
                            )
                            continue

            try:
                result = await dispatch_fn(
                    call_site_id, messages, suppress_dead_letter=True,
                )
                if result.success:
                    await dl_crud.update_status(self.db, item["id"], status="replayed")
                    succeeded += 1
                    logger.info(
                        "Re-dispatched dead letter %s for %s",
                        item["id"], call_site_id,
                    )
                elif _is_unknown_call_site_error(result.error):
                    # The call_site_id no longer exists in the current router
                    # config (almost always: config reload renamed/removed it).
                    # Retrying forever is pure waste — expire immediately.
                    await dl_crud.update_status(self.db, item["id"], status="expired")
                    logger.info(
                        "Expired dead letter %s: call_site_id %r no longer exists in config",
                        item["id"], call_site_id,
                    )
                else:
                    now_iso = self._clock().isoformat()
                    await dl_crud.increment_retry(self.db, item["id"], last_retry_at=now_iso)
                    failed += 1
            except Exception:
                now_iso = self._clock().isoformat()
                await dl_crud.increment_retry(self.db, item["id"], last_retry_at=now_iso)
                failed += 1
                logger.warning(
                    "Re-dispatch failed for dead letter %s", item["id"],
                    exc_info=True,
                )

        if succeeded or failed:
            logger.info(
                "Dead letter redispatch: %d succeeded, %d failed", succeeded, failed,
            )
        return succeeded, failed

    async def get_pending_count(self, *, target_provider: str | None = None) -> int:
        """Count pending items, optionally filtered by provider."""
        return await dl_crud.count_pending(self.db, target_provider=target_provider)
