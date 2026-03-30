"""CRUD operations for outreach_history table."""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    signal_type: str,
    topic: str,
    category: str,
    salience_score: float,
    channel: str,
    message_content: str,
    created_at: str,
    person_id: str | None = None,
    drive_alignment: str | None = None,
    labeled_surplus: int = 0,
    delivery_id: str | None = None,
    content_hash: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO outreach_history
           (id, person_id, signal_type, topic, category, salience_score, channel,
            message_content, drive_alignment, labeled_surplus, delivery_id,
            content_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, person_id, signal_type, topic, category, salience_score, channel,
         message_content, drive_alignment, labeled_surplus, delivery_id,
         content_hash, created_at),
    )
    await db.commit()
    return id


async def upsert(
    db: aiosqlite.Connection,
    *,
    id: str,
    signal_type: str,
    topic: str,
    category: str,
    salience_score: float,
    channel: str,
    message_content: str,
    created_at: str,
    person_id: str | None = None,
    drive_alignment: str | None = None,
    labeled_surplus: int = 0,
    delivery_id: str | None = None,
    content_hash: str | None = None,
) -> str:
    """Idempotent write: insert or update on conflict (outreach_id assigned at queue time)."""
    await db.execute(
        """INSERT INTO outreach_history
           (id, person_id, signal_type, topic, category, salience_score, channel,
            message_content, drive_alignment, labeled_surplus, delivery_id,
            content_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             person_id = excluded.person_id,
             signal_type = excluded.signal_type, topic = excluded.topic,
             category = excluded.category, salience_score = excluded.salience_score,
             channel = excluded.channel, message_content = excluded.message_content,
             drive_alignment = excluded.drive_alignment,
             labeled_surplus = excluded.labeled_surplus,
             delivery_id = excluded.delivery_id,
             content_hash = excluded.content_hash""",
        (id, person_id, signal_type, topic, category, salience_score, channel,
         message_content, drive_alignment, labeled_surplus, delivery_id,
         content_hash, created_at),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM outreach_history WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_by_channel(
    db: aiosqlite.Connection,
    channel: str,
    *,
    person_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    sql = "SELECT * FROM outreach_history WHERE channel = ?"
    params: list = [channel]
    if person_id is not None:
        sql += " AND person_id = ?"
        params.append(person_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def record_engagement(
    db: aiosqlite.Connection,
    id: str,
    *,
    engagement_outcome: str,
    engagement_signal: str | None = None,
    prediction_error: float | None = None,
) -> bool:
    cursor = await db.execute(
        """UPDATE outreach_history SET
           engagement_outcome = ?, engagement_signal = ?, prediction_error = ?
           WHERE id = ?""",
        (engagement_outcome, engagement_signal, prediction_error, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def record_delivery(
    db: aiosqlite.Connection, id: str, *, delivered_at: str
) -> bool:
    cursor = await db.execute(
        "UPDATE outreach_history SET delivered_at = ? WHERE id = ?",
        (delivered_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM outreach_history WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0


async def find_by_delivery_id(
    db: aiosqlite.Connection, delivery_id: str
) -> dict | None:
    """Find outreach record by platform delivery ID."""
    cursor = await db.execute(
        "SELECT * FROM outreach_history WHERE delivery_id = ?",
        (delivery_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_engagement_stats(db: aiosqlite.Connection, *, days: int = 7) -> dict:
    """Return engagement statistics for the last N days.

    Returns: {total, engaged, ignored, ambivalent, pending}
    """
    cursor = await db.execute(
        """SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN engagement_outcome = 'useful' THEN 1 ELSE 0 END) AS engaged,
            SUM(CASE WHEN engagement_outcome = 'ignored' THEN 1 ELSE 0 END) AS ignored,
            SUM(CASE WHEN engagement_outcome = 'ambivalent' THEN 1 ELSE 0 END) AS ambivalent,
            SUM(CASE WHEN engagement_outcome IS NULL THEN 1 ELSE 0 END) AS pending
        FROM outreach_history
        WHERE delivered_at >= datetime('now', ?)""",
        (f"-{days} days",),
    )
    row = await cursor.fetchone()
    if row is None:
        return {"total": 0, "engaged": 0, "ignored": 0, "ambivalent": 0, "pending": 0}
    return {
        "total": row[0] or 0,
        "engaged": row[1] or 0,
        "ignored": row[2] or 0,
        "ambivalent": row[3] or 0,
        "pending": row[4] or 0,
    }


async def find_recent_unengaged(
    db: aiosqlite.Connection, *, hours: int = 4,
) -> list[dict]:
    """Find outreach messages within the last N hours that have no engagement recorded."""
    cursor = await db.execute(
        """SELECT id, delivery_id, delivered_at, category
        FROM outreach_history
        WHERE engagement_outcome IS NULL
          AND delivered_at IS NOT NULL
          AND delivered_at >= datetime('now', ?)
        ORDER BY delivered_at DESC LIMIT 5""",
        (f"-{hours} hours",),
    )
    return [dict(r) for r in await cursor.fetchall()]
