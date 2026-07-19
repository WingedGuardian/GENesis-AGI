"""Queues snapshot — deferred work, dead letters, pending embeddings."""

from __future__ import annotations

import hashlib
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
# The band that was last actually alerted. A same-band re-alert respects the
# cooldown; a band change is an escalation-worthy transition that bypasses it.
_last_dead_letter_band: str = ""

# Rate-based provider-exhaustion STORM alert. The accumulation alert above is
# depth-based (get_stuck_count) and only counts an item once it ages past its
# TTL — so a short-intense storm that self-heals or expires inside the TTL
# window is invisible to it (the observed Apr/Jun storm shape). This catches the
# storm at ENQUEUE RATE: N non-judge dead-letters within a rolling window.
# chain_exhausted:judge (1h self-heal TTL, worthless-late) is excluded so a
# judge burst never pages. Calibrated against real storms: normal operation
# stays <40 non-judge enqueues per 15 min, every observed storm exceeded 45.
# Distinct source so it never collides with the accumulation alert's dedup /
# resolve. Safety-net for provider outages (e.g. a future Groq model EOL).
_DLQ_STORM_WINDOW_S = 900
_DLQ_STORM_THRESHOLD = 40
_DLQ_STORM_COOLDOWN_S = 3600
_DLQ_STORM_JUDGE_PREFIX = "chain_exhausted:judge"
_last_dlq_storm_alert_at: float = 0.0
_last_dlq_storm_band: str = ""


def _dlq_band(count: int) -> str:
    """Bucket the dead-letter count into a stable band.

    The alert's content_hash is derived from the band (not the raw count) so
    minor count drift (310 -> 319 -> 326) does not defeat dedup and produce a
    new observation row per tick. The exact count still appears in the content.
    """
    if count < 100:
        return "50-99"
    if count < 200:
        return "100-199"
    if count < 500:
        return "200-499"
    return "500+"


async def _alert_dead_letter_accumulation(db: aiosqlite.Connection | None, count: int) -> None:
    """Create a critical observation when dead letters accumulate.

    Dedup is band-based and DB-backed (skip_if_duplicate): while an unresolved
    alert exists for the current band, repeated ticks do not create new rows.
    Once the queue drains and _resolve_dead_letter_alerts() resolves them, a
    fresh spike re-alerts.
    """
    global _last_dead_letter_alert_at, _last_dead_letter_band

    now = time.monotonic()
    band = _dlq_band(count)
    # Same-band re-alerts respect the cooldown; a band change (the count crossed
    # a boundary — a worsening or improving transition) is escalation-worthy and
    # bypasses the cooldown so it is not swallowed for up to an hour.
    if (
        now - _last_dead_letter_alert_at < _DEAD_LETTER_ALERT_COOLDOWN_S
        and band == _last_dead_letter_band
    ):
        return  # Cooldown active for the same band

    if db is None:
        return

    try:
        import uuid

        from genesis.db.crud import observations as obs_crud

        content_hash = hashlib.sha256(f"dead_letter_alert:{band}".encode()).hexdigest()
        created = await obs_crud.create(
            db,
            id=str(uuid.uuid4()),
            source="dead_letter_monitor",
            type="infrastructure_alert",
            content=(
                f"Dead letter queue has {count} STUCK items — pending past their "
                f"self-heal TTL (threshold: {_DEAD_LETTER_ALERT_THRESHOLD}). Short-"
                f"TTL self-healing bursts (e.g. chain_exhausted:judge) are excluded, "
                f"so this is a genuine backlog: the replay/expire drainer is failing "
                f"or a provider chain is exhausted. Check circuit breaker state, "
                f"provider health, and the dead_letter_replay surplus task."
            ),
            priority="critical",
            created_at=datetime.now(UTC).isoformat(),
            content_hash=content_hash,
            skip_if_duplicate=True,
        )
        if created is None:
            return  # An unresolved alert for this band already exists.
        _last_dead_letter_alert_at = now
        _last_dead_letter_band = band
        logger.warning(
            "Dead letter alert: %d pending items (critical observation created)", count
        )
    except Exception:
        logger.debug("Failed to create dead letter alert observation", exc_info=True)


async def _resolve_dead_letter_alerts(db: aiosqlite.Connection | None, count: int) -> None:
    """Resolve outstanding dead-letter alerts once the queue drains.

    The observation pipeline is otherwise write-only: _alert_dead_letter_accumulation
    creates infrastructure_alert rows when the queue grows, but nothing resolved
    them when it recovered, so stale "DLQ at 319" alerts lingered for days (3-day
    TTL) and poisoned the morning report. This is the compensating recovery path.
    """
    global _last_dead_letter_alert_at, _last_dead_letter_band

    if db is None:
        return

    # Intentionally unconditional (no in-memory "is an alert active?" guard):
    # such a guard would be lost on restart and reintroduce the write-only
    # staleness. The UPDATE is a cheap no-op when no resolved=0 rows match.
    try:
        from genesis.db.crud import observations as obs_crud

        resolved = await obs_crud.resolve_by_source_and_type(
            db,
            source="dead_letter_monitor",
            type="infrastructure_alert",
            resolved_at=datetime.now(UTC).isoformat(),
            resolution_notes=(
                f"auto-resolved: dead-letter queue drained to {count} "
                f"(< {_DEAD_LETTER_ALERT_THRESHOLD})"
            ),
        )
        if resolved:
            # Genuine recovery — allow an immediate re-alert if it spikes again.
            _last_dead_letter_alert_at = 0.0
            _last_dead_letter_band = ""
            logger.info(
                "Auto-resolved %d dead-letter alert observation(s) on drain (count=%d)",
                resolved,
                count,
            )
    except Exception:
        logger.debug("Failed to resolve dead letter alert observations", exc_info=True)


async def _alert_dead_letter_storm(
    db: aiosqlite.Connection | None,
    count: int,
    breakdown: list[tuple[str, int]],
) -> None:
    """Create a critical observation when dead-letters enqueue at STORM rate.

    Rate-based early warning, distinct from the depth-based accumulation alert:
    fires on ``count`` non-judge dead-letters within the rolling window. Uses a
    distinct ``source="dead_letter_storm"`` AND hash prefix so it never collides
    with the accumulation alert's ``skip_if_duplicate`` dedup or its
    ``resolve_by_source_and_type`` recovery. Same band + cooldown +
    skip_if_duplicate throttle as the accumulation alert, so one storm surfaces
    one alert (a band change bypasses the cooldown as an escalation).
    """
    global _last_dlq_storm_alert_at, _last_dlq_storm_band

    now = time.monotonic()
    band = _dlq_band(count)
    if (
        now - _last_dlq_storm_alert_at < _DLQ_STORM_COOLDOWN_S
        and band == _last_dlq_storm_band
    ):
        return  # Cooldown active for the same band

    if db is None:
        return

    try:
        import uuid

        from genesis.db.crud import observations as obs_crud

        top = ", ".join(
            f"{op.removeprefix('chain_exhausted:')} ×{c}"
            for op, c in breakdown[:5]
        )
        window_min = _DLQ_STORM_WINDOW_S // 60
        content_hash = hashlib.sha256(
            f"dead_letter_storm:{band}".encode()
        ).hexdigest()
        created = await obs_crud.create(
            db,
            id=str(uuid.uuid4()),
            source="dead_letter_storm",
            type="infrastructure_alert",
            content=(
                f"Provider-exhaustion STORM: {count} internal-cognition "
                f"operations dead-lettered in the last {window_min}m (self-"
                f"healing judge calls excluded). Genesis's background thinking "
                f"is failing to run — check provider health and circuit-"
                f"breaker state. Top: {top or 'n/a'}."
            ),
            priority="critical",
            created_at=datetime.now(UTC).isoformat(),
            content_hash=content_hash,
            skip_if_duplicate=True,
        )
        if created is None:
            return  # An unresolved storm alert for this band already exists.
        _last_dlq_storm_alert_at = now
        _last_dlq_storm_band = band
        logger.warning(
            "Dead letter STORM alert: %d non-judge ops in %ds "
            "(critical observation created)",
            count, _DLQ_STORM_WINDOW_S,
        )
    except Exception:
        logger.debug(
            "Failed to create dead letter storm alert observation", exc_info=True
        )


async def _resolve_dead_letter_storm(db: aiosqlite.Connection | None) -> None:
    """Resolve outstanding storm alerts once the enqueue rate returns to normal.

    Mirror of _resolve_dead_letter_alerts for the distinct storm source. Kept
    unconditional (no in-memory guard) so it survives restart; the UPDATE is a
    cheap no-op when nothing matches.
    """
    global _last_dlq_storm_alert_at, _last_dlq_storm_band

    if db is None:
        return

    try:
        from genesis.db.crud import observations as obs_crud

        resolved = await obs_crud.resolve_by_source_and_type(
            db,
            source="dead_letter_storm",
            type="infrastructure_alert",
            resolved_at=datetime.now(UTC).isoformat(),
            resolution_notes="auto-resolved: dead-letter enqueue rate back to normal",
        )
        if resolved:
            _last_dlq_storm_alert_at = 0.0
            _last_dlq_storm_band = ""
            logger.info(
                "Auto-resolved %d dead-letter storm alert observation(s) on rate drop",
                resolved,
            )
    except Exception:
        logger.debug(
            "Failed to resolve dead letter storm alert observations", exc_info=True
        )


async def queues(
    db: aiosqlite.Connection | None,
    deferred_queue: DeferredWorkQueue | None,
    dead_letter: DeadLetterQueue | None,
    event_bus: GenesisEventBus | None = None,
) -> dict:
    errors: list[str] = []

    deferred = None
    # Recovery-backlog subset (what the depth alarm should watch) vs the raw
    # total (honest display). Batch worklists like the dream-synthesis slice
    # legitimately park hundreds of pending items and drain on a cadence — they
    # inflate the raw total but are NOT stuck recovery work, so alerting on the
    # raw total cries wolf on every tick. Mirror the dead_letter raw-vs-stuck
    # split below.
    deferred_recovery = None
    deferred_worklist = 0
    if deferred_queue:
        try:
            deferred = await deferred_queue.count_pending()
            deferred_recovery = await deferred_queue.count_recovery_pending()
            deferred_worklist = await deferred_queue.count_worklist_pending()
        except Exception:
            errors.append("deferred_work: query failed")
            logger.error("Failed to query deferred work queue", exc_info=True)
    else:
        deferred = 0
        deferred_recovery = 0

    dead = None
    if dead_letter:
        try:
            # Raw total for the snapshot/dashboard (honest count of everything
            # pending), but ALERT on the genuinely-STUCK subset only: a fresh
            # burst of short-TTL self-healing items (e.g. chain_exhausted:judge,
            # 1h) drains itself and must not cry wolf on the critical alert.
            dead = await dead_letter.get_pending_count()
            stuck = await dead_letter.get_stuck_count()
            # Alert when STUCK items accumulate; resolve when they clear. Without
            # the resolve, the observation pipeline is write-only and stale
            # "DLQ at N" alerts linger until TTL.
            if stuck >= _DEAD_LETTER_ALERT_THRESHOLD:
                await _alert_dead_letter_accumulation(db, stuck)
            else:
                await _resolve_dead_letter_alerts(db, stuck)

            # Rate-based storm early-warning (independent of depth). Catches a
            # short-intense provider-exhaustion burst that the depth alert above
            # misses because items self-heal / expire before aging past their
            # TTL. Judge (self-healing) is excluded from the trigger count.
            if db is not None:
                from genesis.db.crud import dead_letter as _dl_crud

                since = (
                    datetime.now(UTC) - timedelta(seconds=_DLQ_STORM_WINDOW_S)
                ).isoformat()
                recent = await _dl_crud.count_recent(
                    db, since=since, exclude_prefix=_DLQ_STORM_JUDGE_PREFIX,
                )
                if recent >= _DLQ_STORM_THRESHOLD:
                    breakdown = await _dl_crud.recent_optype_counts(
                        db, since=since, exclude_prefix=_DLQ_STORM_JUDGE_PREFIX,
                    )
                    await _alert_dead_letter_storm(db, recent, breakdown)
                else:
                    await _resolve_dead_letter_storm(db)
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
            # Exclude scheduled batch worklists (dream-synthesis, entity
            # adjudication) from the review sample — they are drained on a daily
            # budget by design and sit pending for days, so surfacing them as
            # "Deferred review" items is a false alarm. They stay counted in the
            # separate `deferred_worklist` gauge. Same segmentation the
            # recovery-depth alarm applies (see BATCH_WORK_TYPES).
            from genesis.resilience.deferred_work import BATCH_WORK_TYPES

            now_dt = datetime.now(UTC)
            placeholders = ",".join("?" * len(BATCH_WORK_TYPES))
            # NOT-IN values are bound parameters; only the placeholder count is
            # interpolated (from an internal constant), so no injection surface.
            cur = await db.execute(
                f"""SELECT work_type, call_site_id, deferred_reason, attempts, created_at
                   FROM deferred_work_queue
                   WHERE status='pending' AND work_type NOT IN ({placeholders})
                   ORDER BY priority, created_at LIMIT 5""",  # noqa: S608
                tuple(BATCH_WORK_TYPES),
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
        # Alarm-eligible subset: genuine recovery backlog, excluding scheduled
        # batch worklists unless they've stalled past a full drain cycle. This
        # is what errors.py:_QUEUE_DEPTH_FIELDS thresholds on (not the raw total).
        "deferred_recovery": deferred_recovery,
        # Batch worklist depth (dream synthesis etc.) — display only, never
        # summed into the depth alarm.
        "deferred_worklist": deferred_worklist,
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
