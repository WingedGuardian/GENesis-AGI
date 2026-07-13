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
    thread_id: str | None = None,
    validated_recipient: str | None = None,
) -> str:
    """Queue a message for bridge delivery. Returns the pending ID.

    ``thread_id`` / ``validated_recipient`` carry the resolved email thread and
    recipient through the queue so the genesis-server drain can rebuild a
    properly-routed request — without them a queued email defaulted to the
    agent's own address (a self-send loop).
    """
    from datetime import UTC, datetime

    pending_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO pending_outreach
           (id, message, category, channel, urgency, deliver_after, created_at,
            thread_id, validated_recipient)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            pending_id,
            message,
            category,
            channel,
            urgency,
            deliver_after,
            now,
            thread_id,
            validated_recipient,
        ),
    )
    await db.commit()
    return pending_id


async def drain(
    db: aiosqlite.Connection,
    *,
    now: str,
) -> list[dict]:
    """Fetch undelivered messages ready for delivery (max 20 per cycle).

    Exposes the always-present ``rowid`` alongside the columns so a row whose
    ``id`` is NULL (legacy rows inserted before ``enqueue`` set a uuid) can
    still be marked delivered by rowid — otherwise ``mark_delivered`` matches
    ``WHERE id = NULL`` (zero rows), the row never clears, and it is
    re-drained every cycle forever (a churn/log-noise loop the 24h age-out
    could not break because the age-out drop *is* a ``mark_delivered`` call).
    """
    cursor = await db.execute(
        """SELECT rowid AS rowid, * FROM pending_outreach
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
    """Mark a pending message as delivered by its ``id`` primary key."""
    cursor = await db.execute(
        "UPDATE pending_outreach SET delivered = 1, delivered_at = ? WHERE id = ?",
        (delivered_at, pending_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def mark_delivered_by_rowid(
    db: aiosqlite.Connection,
    rowid: int,
    *,
    delivered_at: str,
) -> bool:
    """Mark a pending message as delivered by ``rowid``.

    Fallback for rows with a NULL ``id`` (see ``drain``). ``rowid`` is always
    present and unique, so this always targets exactly the intended row.
    """
    cursor = await db.execute(
        "UPDATE pending_outreach SET delivered = 1, delivered_at = ? WHERE rowid = ?",
        (delivered_at, rowid),
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
            id                  TEXT PRIMARY KEY,
            message             TEXT NOT NULL,
            category            TEXT NOT NULL,
            channel             TEXT NOT NULL DEFAULT 'telegram',
            urgency             TEXT NOT NULL DEFAULT 'low',
            deliver_after       TEXT,
            created_at          TEXT NOT NULL,
            delivered           INTEGER NOT NULL DEFAULT 0,
            delivered_at        TEXT,
            thread_id           TEXT,
            validated_recipient TEXT
        )
    """)
    await db.commit()
