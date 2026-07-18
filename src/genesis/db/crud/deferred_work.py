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
        (
            id,
            work_type,
            call_site_id,
            priority,
            payload_json,
            deferred_at,
            deferred_reason,
            staleness_policy,
            staleness_ttl_s,
            status,
            created_at,
        ),
    )
    await db.commit()
    return id


async def delete_by_work_type(
    db: aiosqlite.Connection,
    *,
    work_type: str,
    exclude_status: str | None = "processing",
) -> int:
    """Delete all rows for a work_type, optionally preserving one status.

    Used to supersede an ephemeral batch worklist (e.g. dream-cycle re-clustering
    replaces last week's synthesis worklist and its completed/discarded residue).
    ``exclude_status`` defaults to ``'processing'`` so an in-flight drain item is
    never yanked out from under the worker. Returns the count deleted. This is an
    explicit synchronous supersede — NOT a staleness policy — so it never races
    the recovery ``expire_by_policy`` cadence (which only touches refresh/discard).
    """
    if exclude_status is not None:
        cursor = await db.execute(
            "DELETE FROM deferred_work_queue WHERE work_type = ? AND status != ?",
            (work_type, exclude_status),
        )
    else:
        cursor = await db.execute(
            "DELETE FROM deferred_work_queue WHERE work_type = ?",
            (work_type,),
        )
    await db.commit()
    return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


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


async def count_pending_in(
    db: aiosqlite.Connection,
    *,
    work_types: frozenset[str],
) -> int:
    """Count pending items whose work_type is in ``work_types`` (empty → 0)."""
    if not work_types:
        return 0
    placeholders = ",".join("?" * len(work_types))
    cursor = await db.execute(
        f"SELECT COUNT(*) FROM deferred_work_queue "
        f"WHERE status = 'pending' AND work_type IN ({placeholders})",
        list(work_types),
    )
    row = await cursor.fetchone()
    return int(row[0])


async def count_recovery_pending(
    db: aiosqlite.Connection,
    *,
    batch_work_types: frozenset[str],
    stale_cutoff_iso: str,
) -> int:
    """Count pending items that should trip the recovery-backlog depth alarm.

    Genuine resilience-recovery deferred work always counts. Batch worklist
    items (e.g. the dream-synthesis worklist, which legitimately parks hundreds
    of slices and drains over a cadence) are EXCLUDED — unless they have been
    pending past a full drain cycle (``deferred_at < stale_cutoff_iso``), which
    means the drain has actually stalled and the backlog is real again.

    With no batch types the count is simply all pending (identical to
    ``count_pending``), so callers can pass an empty set to opt out.
    """
    if not batch_work_types:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM deferred_work_queue WHERE status = 'pending'"
        )
        row = await cursor.fetchone()
        return int(row[0])
    placeholders = ",".join("?" * len(batch_work_types))
    cursor = await db.execute(
        f"SELECT COUNT(*) FROM deferred_work_queue WHERE status = 'pending' "
        f"AND (work_type NOT IN ({placeholders}) OR deferred_at < ?)",
        [*batch_work_types, stale_cutoff_iso],
    )
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


async def prune_terminal(db: aiosqlite.Connection, *, cutoff_iso: str) -> int:
    """Delete terminal rows (completed/discarded/expired) finished before *cutoff_iso*.

    Terminal-row retention across ALL work types — the queue had no prune, so
    finished rows accumulated forever (the leak class the entity_adjudication
    orphans were a symptom of). Prunes on ``completed_at`` (when the row actually
    finished), NOT ``created_at`` — a long-pending row that just completed must
    not be swept by an age test against its birth. Rows with a NULL completed_at
    are never pruned here (they are not truly terminal from a timing standpoint).
    Returns the number of rows deleted.
    """
    cursor = await db.execute(
        """DELETE FROM deferred_work_queue
           WHERE status IN ('completed', 'discarded', 'expired')
             AND completed_at IS NOT NULL
             AND completed_at < ?""",
        (cutoff_iso,),
    )
    await db.commit()
    return cursor.rowcount
