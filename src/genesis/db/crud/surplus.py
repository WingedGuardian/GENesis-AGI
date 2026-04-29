"""CRUD operations for surplus_insights table."""

from __future__ import annotations

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


async def purge_expired(db: aiosqlite.Connection) -> int:
    """Discard pending insights past their TTL. Returns count discarded."""
    cursor = await db.execute(
        "UPDATE surplus_insights SET promotion_status = 'discarded' "
        "WHERE promotion_status = 'pending' "
        "AND ttl != '' AND ttl < datetime('now')",
    )
    await db.commit()
    return cursor.rowcount
