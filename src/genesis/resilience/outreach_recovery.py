"""Outreach recovery worker — retries failed Telegram deliveries.

Polls the deferred work queue for ``work_type="outreach_delivery"`` items
and retries via ``OutreachPipeline.submit_raw()``. Exponential backoff
(1m → 5m → 15m → 1h → 1h), max 5 retries. Creates an observation after
exhausting all retries so the failure is visible in the health system.

Scoped to outreach only — NOT reflections, morning reports, or memory ops.
Those have different failure semantics and higher false-positive risk.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from genesis.outreach.pipeline import OutreachPipeline
    from genesis.resilience.deferred_work import DeferredWorkQueue

logger = logging.getLogger(__name__)

# Backoff schedule (seconds) indexed by attempt number (0-based).
# attempt 0 → 60s, 1 → 300s, 2 → 900s, 3 → 3600s, 4 → 3600s
_BACKOFF_SCHEDULE = (60, 300, 900, 3600, 3600)
_MAX_RETRIES = 5
_POLL_INTERVAL_S = 60
_WORK_TYPE = "outreach_delivery"


class OutreachRecoveryWorker:
    """Background worker that retries failed outreach deliveries."""

    def __init__(
        self,
        *,
        queue: DeferredWorkQueue,
        pipeline: OutreachPipeline,
        db: aiosqlite.Connection,
    ) -> None:
        self._queue = queue
        self._pipeline = pipeline
        self._db = db
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the background poll loop."""
        if self._task is not None:
            return
        from genesis.util.tasks import tracked_task
        self._task = tracked_task(self._poll_loop(), name="outreach-recovery")
        logger.info("Outreach recovery worker started")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            logger.info("Outreach recovery worker stopped")

    async def _poll_loop(self) -> None:
        """Poll for pending outreach items and retry them."""
        while True:
            try:
                await asyncio.sleep(_POLL_INTERVAL_S)
                await self._process_pending()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("Outreach recovery poll error", exc_info=True)

    async def _process_pending(self) -> None:
        """Process all pending outreach delivery items that are past their backoff."""
        from genesis.db.crud import deferred_work as crud

        items = await crud.query_pending(
            self._db, work_type=_WORK_TYPE, limit=10,
        )
        if not items:
            return

        now = datetime.now(UTC)
        for item in items:
            attempts = item.get("attempts", 0)

            # Max retries exceeded — discard and create observation
            if attempts >= _MAX_RETRIES:
                await self._exhaust(item)
                continue

            # Backoff check — skip if too soon since last attempt
            last_attempt = item.get("last_attempt_at")
            if last_attempt and attempts > 0:
                try:
                    last_dt = datetime.fromisoformat(last_attempt)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=UTC)
                    backoff_idx = min(attempts - 1, len(_BACKOFF_SCHEDULE) - 1)
                    backoff_s = _BACKOFF_SCHEDULE[backoff_idx]
                    if now < last_dt + timedelta(seconds=backoff_s):
                        continue  # Not yet time to retry
                except (ValueError, TypeError):
                    pass  # Unparseable timestamp — retry now

            await self._retry(item)

    async def _retry(self, item: dict) -> None:
        """Attempt to re-deliver a single outreach item."""
        item_id = item["id"]
        attempts = item.get("attempts", 0)

        try:
            payload = json.loads(item.get("payload_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            logger.error("Outreach recovery: unparseable payload for %s", item_id)
            await self._queue.mark_discarded(item_id, "Unparseable payload")
            return

        # Mark processing (increments attempts + sets last_attempt_at)
        await self._queue.mark_processing(item_id)

        try:
            from genesis.outreach.types import OutreachCategory, OutreachRequest

            category = OutreachCategory(payload.get("category", "alert"))
            request = OutreachRequest(
                category=category,
                topic=payload.get("topic", ""),
                context="",  # Original context not stored — delivery is pre-formatted
                salience_score=0.9,
                signal_type="deferred_retry",
                channel=payload.get("channel"),
            )
            content = payload.get("content", "")
            if not content:
                await self._queue.mark_discarded(item_id, "Empty content")
                return

            result = await self._pipeline.submit_raw(content, request)

            if result.status.value == "delivered":
                await self._queue.mark_completed(item_id)
                logger.info(
                    "Outreach recovery: delivered %s on attempt %d",
                    item_id, attempts + 1,
                )
            else:
                # Delivery failed again
                await self._queue.reset_to_pending(item_id)
                logger.warning(
                    "Outreach recovery: retry %d/%d failed for %s: %s",
                    attempts + 1, _MAX_RETRIES, item_id, result.error or result.status,
                )
        except Exception as exc:
            await self._queue.reset_to_pending(item_id)
            logger.error(
                "Outreach recovery: retry %d/%d exception for %s: %s",
                attempts + 1, _MAX_RETRIES, item_id, exc, exc_info=True,
            )

    async def _exhaust(self, item: dict) -> None:
        """Handle an item that has exhausted all retries."""
        item_id = item["id"]
        payload_str = item.get("payload_json", "{}")

        try:
            payload = json.loads(payload_str)
        except (json.JSONDecodeError, TypeError):
            payload = {}

        reason = (
            f"Outreach delivery exhausted {_MAX_RETRIES} retries: "
            f"channel={payload.get('channel', '?')}, "
            f"category={payload.get('category', '?')}, "
            f"topic={payload.get('topic', '?')[:80]}"
        )

        await self._queue.mark_discarded(item_id, reason)

        # Create observation so the failure surfaces in health/awareness
        try:
            from genesis.db.crud import observations

            content = json.dumps({
                "deferred_id": item_id,
                "channel": payload.get("channel"),
                "category": payload.get("category"),
                "topic": payload.get("topic"),
                "attempts": item.get("attempts", _MAX_RETRIES),
                "original_reason": item.get("deferred_reason", ""),
            })
            content_hash = hashlib.sha256(content.encode()).hexdigest()

            await observations.create(
                self._db,
                id=str(uuid.uuid4()),
                source="outreach_recovery",
                type="delivery_exhausted",
                content=content,
                priority="high",
                created_at=datetime.now(UTC).isoformat(),
                content_hash=content_hash,
                skip_if_duplicate=True,
            )
        except Exception:
            logger.error(
                "Failed to create observation for exhausted outreach %s",
                item_id, exc_info=True,
            )

        logger.error(
            "Outreach delivery EXHAUSTED all %d retries: %s",
            _MAX_RETRIES, reason,
        )
