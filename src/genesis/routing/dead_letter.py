"""Dead-letter queue for failed routing operations."""

from __future__ import annotations

import ast
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.db.crud import dead_letter as dl_crud
from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem

logger = logging.getLogger(__name__)


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
                result = await dispatch_fn(call_site_id, messages)
                if result.success:
                    await dl_crud.update_status(self.db, item["id"], status="replayed")
                    succeeded += 1
                    logger.info(
                        "Re-dispatched dead letter %s for %s",
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
