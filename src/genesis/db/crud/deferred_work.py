"""CRUD operations for deferred_work_queue table."""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    work_type: str,
    priority: int,
    payload_json: str,
    deferred_at: str,
    deferred_reason: str,
    created_at: str,
    call_site_id: str | None = None,
    staleness_policy: str = "drain",
    staleness_ttl_s: int | None = None,
    status: str = "pending",
) -> str:
    await db.execute(
        """INSERT INTO deferred_work_queue
           (id, work_type, call_site_id, priority, payload_json, deferred_at,
            deferred_reason, staleness_policy, staleness_ttl_s, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, work_type, call_site_id, priority, payload_json, deferred_at,
         deferred_reason, staleness_policy, staleness_ttl_s, status, created_at),
    )
    await db.commit()
    return id


async def query_pending(
    db: aiosqlite.Connection,
    *,
    work_type: str | None = None,
    max_priority: int = 100,
    limit: int = 50,
) -> list[dict]:
    sql = "SELECT * FROM deferred_work_queue WHERE status = 'pending' AND priority <= ?"
    params: list = [max_priority]
    if work_type is not None:
        sql += " AND work_type = ?"
        params.append(work_type)
    sql += " ORDER BY priority ASC, deferred_at ASC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def update_status(
    db: aiosqlite.Connection,
    id: str,
    *,
    status: str,
    error_message: str | None = None,
    completed_at: str | None = None,
    last_attempt_at: str | None = None,
) -> bool:
    parts = ["status = ?"]
    params: list = [status]
    if error_message is not None:
        parts.append("error_message = ?")
        params.append(error_message)
    if completed_at is not None:
        parts.append("completed_at = ?")
        params.append(completed_at)
    if last_attempt_at is not None:
        parts.append("last_attempt_at = ?")
        params.append(last_attempt_at)
        parts.append("attempts = attempts + 1")
    params.append(id)
    cursor = await db.execute(
        f"UPDATE deferred_work_queue SET {', '.join(parts)} WHERE id = ?",
        params,
    )
    await db.commit()
    return cursor.rowcount > 0


async def count_pending(
    db: aiosqlite.Connection,
    *,
    work_type: str | None = None,
) -> int:
    sql = "SELECT COUNT(*) FROM deferred_work_queue WHERE status = 'pending'"
    params: list = []
    if work_type is not None:
        sql += " AND work_type = ?"
        params.append(work_type)
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return int(row[0])


async def drain_by_priority(
    db: aiosqlite.Connection,
    *,
    limit: int = 10,
) -> list[dict]:
    cursor = await db.execute(
        """SELECT * FROM deferred_work_queue
           WHERE status = 'pending'
           ORDER BY priority ASC, deferred_at ASC
           LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def expire_by_policy(
    db: aiosqlite.Connection,
    *,
    now_iso: str,
) -> int:
    """Expire items based on staleness policy. Returns count of expired items."""
    # refresh and discard policies always expire
    cursor = await db.execute(
        """UPDATE deferred_work_queue
           SET status = 'expired', completed_at = ?
           WHERE status = 'pending'
           AND staleness_policy IN ('refresh', 'discard')""",
        (now_iso,),
    )
    expired = cursor.rowcount

    # ttl policy: expire if older than staleness_ttl_s
    cursor = await db.execute(
        """UPDATE deferred_work_queue
           SET status = 'expired', completed_at = ?
           WHERE status = 'pending'
           AND staleness_policy = 'ttl'
           AND staleness_ttl_s IS NOT NULL
           AND datetime(deferred_at, '+' || staleness_ttl_s || ' seconds') <= datetime(?)""",
        (now_iso, now_iso),
    )
    expired += cursor.rowcount

    await db.commit()
    return expired


async def expire_stuck_processing(
    db: aiosqlite.Connection,
    *,
    max_age_hours: int = 2,
) -> int:
    """Reset items stuck in 'processing' for too long back to 'pending'.

    Items get orphaned in 'processing' when the process is killed (e.g.,
    bridge restart) mid-execution.  The work didn't fail — the process
    was killed — so resetting to 'pending' allows retry on next cycle.

    Returns count of items reset.
    """
    from datetime import UTC, datetime, timedelta

    cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
    cursor = await db.execute(
        """UPDATE deferred_work_queue
           SET status = 'pending'
           WHERE status = 'processing'
           AND last_attempt_at < ?""",
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount


async def query_failed(
    db: aiosqlite.Connection,
    *,
    since: str | None = None,
    work_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return items that failed: have error_message or status expired/discarded."""
    sql = """SELECT * FROM deferred_work_queue
             WHERE (error_message IS NOT NULL
                    OR status IN ('expired', 'discarded'))"""
    params: list = []
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    if work_type:
        sql += " AND work_type = ?"
        params.append(work_type)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]
