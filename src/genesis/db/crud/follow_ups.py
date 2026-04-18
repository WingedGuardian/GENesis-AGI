"""CRUD operations for follow_ups table — the accountability ledger."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import aiosqlite


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


async def create(
    db: aiosqlite.Connection,
    *,
    content: str,
    source: str,
    strategy: str,
    reason: str | None = None,
    source_session: str | None = None,
    scheduled_at: str | None = None,
    priority: str = "medium",
    id: str | None = None,
) -> str:
    """Create a follow-up and return its ID."""
    fid = id or _new_id()
    await db.execute(
        """INSERT INTO follow_ups
           (id, source, source_session, content, reason, strategy,
            scheduled_at, status, priority, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (fid, source, source_session, content, reason, strategy,
         scheduled_at, priority, _now_iso()),
    )
    await db.commit()
    return fid


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM follow_ups WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_pending(
    db: aiosqlite.Connection,
    *,
    source: str | None = None,
    strategy: str | None = None,
) -> list[dict]:
    """Get pending follow-ups, optionally filtered by source/strategy."""
    query = "SELECT * FROM follow_ups WHERE status = 'pending'"
    params: list[str] = []
    if source is not None:
        query += " AND source = ?"
        params.append(source)
    if strategy is not None:
        query += " AND strategy = ?"
        params.append(strategy)
    query += " ORDER BY created_at ASC"
    cursor = await db.execute(query, params)
    return [dict(row) for row in await cursor.fetchall()]


async def get_by_status(
    db: aiosqlite.Connection,
    status: str,
) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM follow_ups WHERE status = ? ORDER BY created_at ASC",
        (status,),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def get_actionable(db: aiosqlite.Connection, *, limit: int = 50) -> list[dict]:
    """Get follow-ups needing attention: pending, failed, blocked.

    Capped at `limit` to prevent unbounded growth from flooding contexts.
    """
    cursor = await db.execute(
        "SELECT * FROM follow_ups WHERE status IN ('pending', 'failed', 'blocked') "
        "ORDER BY CASE priority "
        "  WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "  WHEN 'medium' THEN 2 ELSE 3 END, created_at ASC "
        "LIMIT ?",
        (limit,),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def get_scheduled_due(db: aiosqlite.Connection) -> list[dict]:
    """Get scheduled follow-ups whose time has arrived."""
    cursor = await db.execute(
        "SELECT * FROM follow_ups "
        "WHERE strategy = 'scheduled_task' AND status = 'pending' "
        "AND scheduled_at IS NOT NULL "
        "AND scheduled_at <= strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') "
        "ORDER BY scheduled_at ASC",
    )
    return [dict(row) for row in await cursor.fetchall()]


async def get_linked_active(db: aiosqlite.Connection) -> list[dict]:
    """Get follow-ups linked to surplus tasks that are in flight."""
    cursor = await db.execute(
        "SELECT * FROM follow_ups "
        "WHERE linked_task_id IS NOT NULL "
        "AND status IN ('scheduled', 'in_progress') "
        "ORDER BY created_at ASC",
    )
    return [dict(row) for row in await cursor.fetchall()]


async def update_status(
    db: aiosqlite.Connection,
    id: str,
    status: str,
    *,
    resolution_notes: str | None = None,
    blocked_reason: str | None = None,
    verified_at: str | None = None,
    verification_notes: str | None = None,
) -> bool:
    """Update follow-up status. Sets completed_at on terminal states."""
    parts = ["status = ?"]
    params: list[str | None] = [status]
    if status in ("completed", "failed"):
        parts.append("completed_at = ?")
        params.append(_now_iso())
    if resolution_notes is not None:
        parts.append("resolution_notes = ?")
        params.append(resolution_notes)
    if blocked_reason is not None:
        parts.append("blocked_reason = ?")
        params.append(blocked_reason)
    if verified_at is not None:
        parts.append("verified_at = ?")
        params.append(verified_at)
    if verification_notes is not None:
        parts.append("verification_notes = ?")
        params.append(verification_notes)
    params.append(id)
    cursor = await db.execute(
        f"UPDATE follow_ups SET {', '.join(parts)} WHERE id = ?",
        params,
    )
    await db.commit()
    return cursor.rowcount > 0


async def link_task(
    db: aiosqlite.Connection,
    id: str,
    surplus_task_id: str,
) -> bool:
    """Link a follow-up to a surplus task and mark as scheduled."""
    cursor = await db.execute(
        "UPDATE follow_ups SET linked_task_id = ?, status = 'scheduled' WHERE id = ?",
        (surplus_task_id, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def escalate(
    db: aiosqlite.Connection,
    id: str,
    target: str,
) -> bool:
    """Mark follow-up as escalated to ego or promoted to task."""
    cursor = await db.execute(
        "UPDATE follow_ups SET escalated_to = ? WHERE id = ?",
        (target, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_summary_counts(db: aiosqlite.Connection) -> dict[str, int]:
    """Get counts by status for dashboard badges."""
    cursor = await db.execute(
        "SELECT status, COUNT(*) FROM follow_ups GROUP BY status"
    )
    return {row[0]: row[1] for row in await cursor.fetchall()}


async def get_recent(
    db: aiosqlite.Connection,
    *,
    limit: int = 20,
) -> list[dict]:
    """Get recent follow-ups for dashboard display."""
    cursor = await db.execute(
        "SELECT * FROM follow_ups ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(row) for row in await cursor.fetchall()]
