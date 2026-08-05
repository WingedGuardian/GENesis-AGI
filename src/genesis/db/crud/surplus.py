"""CRUD operations for surplus_insights table."""

from __future__ import annotations

from collections.abc import Sequence

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    content: str,
    source_task_type: str,
    generating_model: str,
    drive_alignment: str,
    created_at: str,
    ttl: str,
    confidence: float = 0.0,
    engagement_prediction: float | None = None,
) -> str:
    await db.execute(
        """INSERT INTO surplus_insights
           (id, content, source_task_type, generating_model, drive_alignment,
            confidence, engagement_prediction, created_at, ttl)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, content, source_task_type, generating_model, drive_alignment,
         confidence, engagement_prediction, created_at, ttl),
    )
    await db.commit()
    return id


async def upsert(
    db: aiosqlite.Connection,
    *,
    id: str,
    content: str,
    source_task_type: str,
    generating_model: str,
    drive_alignment: str,
    created_at: str,
    ttl: str,
    confidence: float = 0.0,
    engagement_prediction: float | None = None,
) -> str:
    """Idempotent write: insert or update on conflict."""
    await db.execute(
        """INSERT INTO surplus_insights
           (id, content, source_task_type, generating_model, drive_alignment,
            confidence, engagement_prediction, created_at, ttl)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             content = excluded.content, source_task_type = excluded.source_task_type,
             generating_model = excluded.generating_model,
             drive_alignment = excluded.drive_alignment,
             confidence = excluded.confidence,
             engagement_prediction = excluded.engagement_prediction,
             ttl = excluded.ttl""",
        (id, content, source_task_type, generating_model, drive_alignment,
         confidence, engagement_prediction, created_at, ttl),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM surplus_insights WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_pending(db: aiosqlite.Connection, *, limit: int = 50) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM surplus_insights WHERE promotion_status = 'pending' "
        "AND confidence > 0.0 "
        "ORDER BY confidence DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def count_pending(db: aiosqlite.Connection) -> int:
    """Return the total number of pending surplus insights."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM surplus_insights WHERE promotion_status = 'pending' "
        "AND confidence > 0.0",
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def list_promotable_ideas(
    db: aiosqlite.Connection,
    *,
    task_types: Sequence[str],
    limit: int,
) -> list[dict]:
    """Pending, unexpired staged ideation rows eligible for promotion into the
    follow_ups 'idea' review lane (WS-M PR-2).

    Filtered to ``task_types`` (surplus_insights is a shared table — only genuine
    IDEA task types graduate) and ttl-unexpired (matching purge_expired's parser).
    Ordered oldest-first (FIFO): curated ideas all tie at ~0.6 confidence, so
    created_at order is the fair graduation order and guarantees every item is
    seen before it decays when capacity suffices.
    """
    if not task_types:
        return []
    placeholders = ", ".join("?" for _ in task_types)
    cursor = await db.execute(
        "SELECT * FROM surplus_insights "
        "WHERE promotion_status = 'pending' "
        f"AND source_task_type IN ({placeholders}) "  # noqa: S608 - bound params
        "AND (ttl = '' OR datetime(REPLACE(ttl, 'T', ' ')) >= datetime('now')) "
        "ORDER BY created_at ASC LIMIT ?",
        (*task_types, limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def promote(db: aiosqlite.Connection, id: str, *, promoted_to: str) -> bool:
    cursor = await db.execute(
        "UPDATE surplus_insights SET promotion_status = 'promoted', promoted_to = ? WHERE id = ?",
        (promoted_to, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def discard(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute(
        "UPDATE surplus_insights SET promotion_status = 'discarded' WHERE id = ?",
        (id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM surplus_insights WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0


async def list_promoted(
    db: aiosqlite.Connection,
    *,
    limit: int = 20,
    unconsumed_only: bool = True,
) -> list[dict]:
    """Return promoted insights, optionally only those not yet consumed by reflection."""
    if unconsumed_only:
        cursor = await db.execute(
            "SELECT * FROM surplus_insights "
            "WHERE promotion_status = 'promoted' AND consumed_at IS NULL "
            "ORDER BY confidence DESC LIMIT ?",
            (limit,),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM surplus_insights "
            "WHERE promotion_status = 'promoted' "
            "ORDER BY confidence DESC LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in await cursor.fetchall()]


async def mark_consumed_batch(
    db: aiosqlite.Connection,
    ids: list[str],
    *,
    consumed_at: str | None = None,
) -> int:
    """Mark promoted insights as consumed by reflection. Returns count updated."""
    if not ids:
        return 0
    from datetime import UTC, datetime

    ts = consumed_at or datetime.now(UTC).isoformat()
    placeholders = ",".join("?" for _ in ids)
    cursor = await db.execute(
        f"UPDATE surplus_insights SET consumed_at = ? "  # noqa: S608
        f"WHERE id IN ({placeholders}) AND promotion_status = 'promoted'",
        [ts, *ids],
    )
    await db.commit()
    return cursor.rowcount


async def purge_expired(db: aiosqlite.Connection) -> int:
    """Discard pending insights past their TTL. Returns count discarded."""
    cursor = await db.execute(
        "UPDATE surplus_insights SET promotion_status = 'discarded' "
        "WHERE promotion_status = 'pending' "
        "AND ttl != '' AND datetime(REPLACE(ttl, 'T', ' ')) < datetime('now')",
    )
    await db.commit()
    return cursor.rowcount


async def purge_discarded(db: aiosqlite.Connection, *, older_than_days: int = 30) -> int:
    """Hard-delete discarded insights older than a cutoff. Returns count deleted.

    Bounds accumulation of inert discarded rows (purge_expired only flips
    pending→discarded; nothing removed them). Gated on created_at (no discarded_at
    column) — conservative: a row discarded at its 7d TTL survives ~older_than_days
    past creation, i.e. slightly longer than the nominal window. Promoted/pending
    rows are never touched.
    """
    cursor = await db.execute(
        "DELETE FROM surplus_insights WHERE promotion_status = 'discarded' "
        "AND created_at != '' "
        "AND datetime(REPLACE(created_at, 'T', ' ')) < datetime('now', ?)",
        (f"-{older_than_days} days",),
    )
    await db.commit()
    return cursor.rowcount
