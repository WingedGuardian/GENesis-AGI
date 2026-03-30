"""CRUD operations for pending_outreach table.

Foreground CC sessions write here; the bridge drains it via the outreach pipeline.
"""

from __future__ import annotations

import uuid

import aiosqlite


async def enqueue(
    db: aiosqlite.Connection,
    *,
    message: str,
    category: str,
    channel: str = "telegram",
    urgency: str = "low",
    deliver_after: str | None = None,
) -> str:
    """Queue a message for bridge delivery. Returns the pending ID."""
    from datetime import UTC, datetime

    pending_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO pending_outreach
           (id, message, category, channel, urgency, deliver_after, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (pending_id, message, category, channel, urgency, deliver_after, now),
    )
    await db.commit()
    return pending_id


async def drain(
    db: aiosqlite.Connection,
    *,
    now: str,
) -> list[dict]:
    """Fetch undelivered messages ready for delivery (max 20 per cycle)."""
    cursor = await db.execute(
        """SELECT * FROM pending_outreach
           WHERE delivered = 0
             AND (deliver_after IS NULL OR deliver_after <= ?)
           ORDER BY created_at ASC
           LIMIT 20""",
        (now,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def mark_delivered(
    db: aiosqlite.Connection,
    pending_id: str,
    *,
    delivered_at: str,
) -> bool:
    """Mark a pending message as delivered."""
    cursor = await db.execute(
        "UPDATE pending_outreach SET delivered = 1, delivered_at = ? WHERE id = ?",
        (delivered_at, pending_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def ensure_table(db: aiosqlite.Connection) -> None:
    """Create the pending_outreach table if it doesn't exist.

    Called by the MCP standalone fallback to ensure the table exists
    without requiring a full bootstrap.
    """
    await db.execute("""
        CREATE TABLE IF NOT EXISTS pending_outreach (
            id              TEXT PRIMARY KEY,
            message         TEXT NOT NULL,
            category        TEXT NOT NULL,
            channel         TEXT NOT NULL DEFAULT 'telegram',
            urgency         TEXT NOT NULL DEFAULT 'low',
            deliver_after   TEXT,
            created_at      TEXT NOT NULL,
            delivered       INTEGER NOT NULL DEFAULT 0,
            delivered_at    TEXT
        )
    """)
    await db.commit()
