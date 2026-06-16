"""Queues snapshot — deferred work, dead letters, pending embeddings."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from genesis.observability.events import GenesisEventBus
    from genesis.resilience.deferred_work import DeferredWorkQueue
    from genesis.routing.dead_letter import DeadLetterQueue

logger = logging.getLogger(__name__)

# Dead letter accumulation alerting — creates a critical observation when
# pending items exceed this threshold. Checked on every awareness tick.
_DEAD_LETTER_ALERT_THRESHOLD = 50
# Cooldown prevents alert spam — one observation per hour max.
_DEAD_LETTER_ALERT_COOLDOWN_S = 3600
_last_dead_letter_alert_at: float = 0.0


async def _alert_dead_letter_accumulation(db: aiosqlite.Connection | None, count: int) -> None:
    """Create a critical observation when dead letters accumulate."""
    global _last_dead_letter_alert_at

    now = time.monotonic()
    if now - _last_dead_letter_alert_at < _DEAD_LETTER_ALERT_COOLDOWN_S:
        return  # Cooldown active

    if db is None:
        return

    try:
        import uuid

        from genesis.db.crud import observations as obs_crud

        obs_id = str(uuid.uuid4())
        await obs_crud.create(
            db,
            id=obs_id,
            source="dead_letter_monitor",
            type="infrastructure_alert",
            content=(
                f"Dead letter queue has {count} pending items (threshold: "
                f"{_DEAD_LETTER_ALERT_THRESHOLD}). Likely cause: provider "
                f"chain exhaustion or circuit breaker stuck open. "
                f"Check circuit breaker state and provider health."
            ),
            priority="critical",
            created_at=datetime.now(UTC).isoformat(),
        )
        _last_dead_letter_alert_at = now
        logger.warning(
            "Dead letter alert: %d pending items (critical observation created)", count
        )
    except Exception:
        logger.debug("Failed to create dead letter alert observation", exc_info=True)


async def queues(
    db: aiosqlite.Connection | None,
    deferred_queue: DeferredWorkQueue | None,
    dead_letter: DeadLetterQueue | None,
    event_bus: GenesisEventBus | None = None,
) -> dict:
    errors: list[str] = []

    deferred = None
    if deferred_queue:
        try:
            deferred = await deferred_queue.count_pending()
        except Exception:
            errors.append("deferred_work: query failed")
            logger.error("Failed to query deferred work queue", exc_info=True)
    else:
        deferred = 0

    dead = None
    if dead_letter:
        try:
            dead = await dead_letter.get_pending_count()
            # Alert via observation when dead letters accumulate
            if dead is not None and dead >= _DEAD_LETTER_ALERT_THRESHOLD:
                await _alert_dead_letter_accumulation(db, dead)
        except Exception:
            errors.append("dead_letters: query failed")
            logger.error("Failed to query dead letter queue", exc_info=True)
    else:
        dead = 0

    embeddings = None
    if db:
        try:
            from genesis.db.crud import pending_embeddings

            embeddings = await pending_embeddings.count_pending(db)
        except Exception:
            errors.append("pending_embeddings: query failed")
            logger.error("Failed to count pending embeddings", exc_info=True)
    else:
        embeddings = 0

    dead_letter_oldest_s = None
    deferred_oldest_s = None
    if db:
        try:
            now = datetime.now(UTC)
            cur = await db.execute(
                "SELECT MIN(created_at) as oldest FROM dead_letter WHERE status='pending'"
            )
            row = await cur.fetchone()
            if row and row["oldest"]:
                dead_letter_oldest_s = round(
                    (now - datetime.fromisoformat(row["oldest"])).total_seconds(), 1
                )
        except Exception as exc:
            logger.warning("Failed to query dead letter oldest age: %s", exc, exc_info=True)
        try:
            cur = await db.execute(
                "SELECT MIN(created_at) as oldest FROM deferred_work_queue WHERE status='pending'"
            )
            row = await cur.fetchone()
            if row and row["oldest"]:
                deferred_oldest_s = round(
                    (now - datetime.fromisoformat(row["oldest"])).total_seconds(), 1
                )
        except Exception as exc:
            logger.warning("Failed to query deferred work oldest age: %s", exc, exc_info=True)

    deferred_items: list[dict] = []
    if db:
        try:
            now_dt = datetime.now(UTC)
            cur = await db.execute(
                """SELECT work_type, call_site_id, deferred_reason, attempts, created_at
                   FROM deferred_work_queue WHERE status='pending'
                   ORDER BY priority, created_at LIMIT 5"""
            )
            for row in await cur.fetchall():
                age = (now_dt - datetime.fromisoformat(row["created_at"])).total_seconds()
                deferred_items.append({
                    "type": row["work_type"],
                    "site": row["call_site_id"],
                    "reason": row["deferred_reason"],
                    "attempts": row["attempts"],
                    "age_s": round(age),
                })
        except Exception:
            logger.warning("Failed to query deferred item details", exc_info=True)

    discarded_items: list[dict] = []
    discarded_count = 0
    if db:
        try:
            now_dt2 = datetime.now(UTC)
            cur = await db.execute(
                "SELECT COUNT(*) FROM deferred_work_queue WHERE status IN ('discarded', 'expired')"
            )
            row = await cur.fetchone()
            discarded_count = row[0] if row else 0

            cur = await db.execute(
                """SELECT id, work_type, call_site_id, deferred_reason, attempts,
                          error_message, status, created_at, completed_at
                   FROM deferred_work_queue WHERE status IN ('discarded', 'expired')
                   ORDER BY completed_at DESC LIMIT 20"""
            )
            for row in await cur.fetchall():
                age = (now_dt2 - datetime.fromisoformat(row["created_at"])).total_seconds()
                discarded_items.append({
                    "id": row["id"],
                    "type": row["work_type"],
                    "site": row["call_site_id"],
                    "reason": row["deferred_reason"],
                    "error": row["error_message"],
                    "status": row["status"],
                    "attempts": row["attempts"],
                    "age_s": round(age),
                    "discarded_at": row["completed_at"],
                })
        except Exception:
            logger.warning("Failed to query discarded deferred items", exc_info=True)

    deferred_processing = 0
    deferred_stuck = 0
    if db:
        try:
            cur = await db.execute(
                "SELECT COUNT(*) FROM deferred_work_queue WHERE status = 'processing'"
            )
            row = await cur.fetchone()
            deferred_processing = row[0] if row else 0

            stuck_cutoff = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
            cur = await db.execute(
                "SELECT COUNT(*) FROM deferred_work_queue "
                "WHERE status = 'processing' AND last_attempt_at < ?",
                (stuck_cutoff,),
            )
            row = await cur.fetchone()
            deferred_stuck = row[0] if row else 0
        except Exception:
            logger.warning("Failed to query processing/stuck deferred items", exc_info=True)

    failed_embeddings = 0
    embedded_total = 0
    embedding_last_error = None
    if db:
        try:
            cur = await db.execute(
                "SELECT COUNT(*) FROM pending_embeddings WHERE status = 'failed'"
            )
            row = await cur.fetchone()
            failed_embeddings = row[0] if row else 0

            cur = await db.execute(
                "SELECT COUNT(*) FROM pending_embeddings WHERE status = 'embedded'"
            )
            row = await cur.fetchone()
            embedded_total = row[0] if row else 0

            if failed_embeddings > 0:
                cur = await db.execute(
                    "SELECT error_message FROM pending_embeddings "
                    "WHERE status = 'failed' ORDER BY created_at DESC LIMIT 1"
                )
                row = await cur.fetchone()
                if row and row["error_message"]:
                    embedding_last_error = row["error_message"][:120]
        except Exception:
            logger.warning("Failed to query embedding metrics", exc_info=True)

    result = {
        "deferred_work": deferred,
        "deferred_oldest_age_seconds": deferred_oldest_s,
        "deferred_processing": deferred_processing,
        "deferred_stuck": deferred_stuck,
        "dead_letters": dead,
        "dead_letter_oldest_age_seconds": dead_letter_oldest_s,
        "pending_embeddings": embeddings,
        "failed_embeddings": failed_embeddings,
        "embedded_total": embedded_total,
        "embedding_last_error": embedding_last_error,
        "deferred_items": deferred_items,
        "discarded_count": discarded_count,
        "discarded_items": discarded_items,
        # WS-17: cumulative count of events dropped because the event-bus
        # persistence queue was full (0 when persistence/bus is unavailable).
        # NOTE: a cumulative counter, not an instantaneous depth — deliberately
        # NOT in errors.py:_QUEUE_DEPTH_FIELDS (which alerts on depth > 100).
        "events_dropped": (
            event_bus.dropped_event_count() if event_bus is not None else 0
        ),
    }
    if errors:
        result["errors"] = errors
    return result
