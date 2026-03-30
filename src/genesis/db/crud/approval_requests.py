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
           WHERE id = ?""",
        (status, resolved_at, resolved_by, id),
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


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute(
        "DELETE FROM approval_requests WHERE id = ?", (id,)
    )
    await db.commit()
    return cursor.rowcount > 0
