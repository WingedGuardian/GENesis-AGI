"""CRUD for ``pending_email_sends`` — the WS-8 email autonomy gate hold store.

A row is written when the gate holds an outbound email (capability cell not
GRANTED).  The resolution watcher drains ``status='held'`` rows: on approval it
sends below the gate and marks 'sent'; on rejection/timeout it marks
'rejected'/'expired'.  ``mark_sent``/``mark_rejected`` gate on
``WHERE status='held'`` so a row can leave 'held' exactly once (double-send
guard, alongside the ``request_id`` UNIQUE constraint).
"""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    request_id: str,
    validated_recipient: str,
    category: str,
    message: str,
    cell_domain: str,
    cell_verb: str,
    cell_risk_class: str,
    held_at: str,
    channel: str = "email",
    thread_id: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO pending_email_sends
             (id, request_id, thread_id, validated_recipient, channel, category,
              message, cell_domain, cell_verb, cell_risk_class, held_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'held')""",
        (id, request_id, thread_id, validated_recipient, channel, category,
         message, cell_domain, cell_verb, cell_risk_class, held_at),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM pending_email_sends WHERE id = ?", (id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_by_request(db: aiosqlite.Connection, request_id: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM pending_email_sends WHERE request_id = ?", (request_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_held(db: aiosqlite.Connection) -> list[dict]:
    """All rows still awaiting resolution — the drain's work list."""
    cursor = await db.execute(
        "SELECT * FROM pending_email_sends WHERE status = 'held' ORDER BY held_at"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def mark_sent(db: aiosqlite.Connection, id: str, *, sent_at: str) -> bool:
    """Transition held → sent. Returns False if the row already left 'held'
    (double-send guard: only one caller can flip a given hold)."""
    cursor = await db.execute(
        "UPDATE pending_email_sends SET status = 'sent', sent_at = ? "
        "WHERE id = ? AND status = 'held'",
        (sent_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def mark_rejected(
    db: aiosqlite.Connection, id: str, *, rejected_at: str, expired: bool = False
) -> bool:
    """Transition held → rejected (or expired). Returns False if not still held."""
    status = "expired" if expired else "rejected"
    cursor = await db.execute(
        "UPDATE pending_email_sends SET status = ?, rejected_at = ? "
        "WHERE id = ? AND status = 'held'",
        (status, rejected_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0
