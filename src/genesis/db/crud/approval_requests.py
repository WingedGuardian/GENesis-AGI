"""CRUD operations for approval_requests table."""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    action_type: str,
    action_class: str,
    description: str,
    context: str | None = None,
    status: str = "pending",
    timeout_at: str | None = None,
    created_at: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO approval_requests
           (id, action_type, action_class, description, context,
            status, timeout_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))""",
        (id, action_type, action_class, description, context,
         status, timeout_at, created_at),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM approval_requests WHERE id = ?", (id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_pending(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute(
        """SELECT * FROM approval_requests
           WHERE status = 'pending'
           ORDER BY created_at ASC""",
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_recent(
    db: aiosqlite.Connection, *, limit: int = 200,
) -> list[dict]:
    cursor = await db.execute(
        """SELECT * FROM approval_requests
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_expired(
    db: aiosqlite.Connection, *, now: str
) -> list[dict]:
    """Find pending requests whose timeout has passed."""
    cursor = await db.execute(
        """SELECT * FROM approval_requests
           WHERE status = 'pending'
             AND timeout_at IS NOT NULL
             AND timeout_at <= ?
           ORDER BY timeout_at ASC""",
        (now,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def resolve(
    db: aiosqlite.Connection,
    id: str,
    *,
    status: str,
    resolved_at: str,
    resolved_by: str | None = None,
) -> bool:
    """Resolve a request (approve, reject, expire, cancel)."""
    cursor = await db.execute(
        """UPDATE approval_requests
           SET status = ?, resolved_at = ?, resolved_by = ?
           WHERE id = ?
             AND status = 'pending'""",
        (status, resolved_at, resolved_by, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def update_context(
    db: aiosqlite.Connection, id: str, *, context: str,
) -> bool:
    cursor = await db.execute(
        """UPDATE approval_requests
           SET context = ?
           WHERE id = ?""",
        (context, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def expire_timed_out(
    db: aiosqlite.Connection, *, now: str
) -> int:
    """Bulk-expire all pending requests past their timeout. Returns count."""
    cursor = await db.execute(
        """UPDATE approval_requests
           SET status = 'expired', resolved_at = ?
           WHERE status = 'pending'
             AND timeout_at IS NOT NULL
             AND timeout_at <= ?""",
        (now, now),
    )
    await db.commit()
    return cursor.rowcount


async def mark_consumed(
    db: aiosqlite.Connection, id: str, *, consumed_at: str,
) -> bool:
    """Mark an approved request as consumed (action was dispatched).

    Atomic: only updates if consumed_at IS NULL, preventing double-dispatch.
    Returns True if this call consumed it, False if already consumed.
    """
    cursor = await db.execute(
        """UPDATE approval_requests
           SET consumed_at = ?
           WHERE id = ?
             AND status = 'approved'
             AND consumed_at IS NULL""",
        (consumed_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def find_approved_unconsumed(
    db: aiosqlite.Connection,
    *,
    subsystem: str,
    policy_id: str,
) -> dict | None:
    """Find an approved request that hasn't been consumed yet.

    Used by the resume mechanism: when an approval is granted (via Telegram
    or dashboard), the blocked action can resume on the next tick.
    """
    cursor = await db.execute(
        """SELECT * FROM approval_requests
           WHERE status = 'approved'
             AND consumed_at IS NULL
             AND json_extract(context, '$.subsystem') = ?
             AND json_extract(context, '$.policy_id') = ?
           ORDER BY resolved_at DESC
           LIMIT 1""",
        (subsystem, policy_id),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute(
        "DELETE FROM approval_requests WHERE id = ?", (id,)
    )
    await db.commit()
    return cursor.rowcount > 0
