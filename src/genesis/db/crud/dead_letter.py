"""CRUD operations for dead_letter table."""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    operation_type: str,
    payload: str,
    target_provider: str,
    failure_reason: str,
    created_at: str,
    status: str = "pending",
) -> str:
    await db.execute(
        """INSERT INTO dead_letter
           (id, operation_type, payload, target_provider, failure_reason, created_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (id, operation_type, payload, target_provider, failure_reason, created_at, status),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM dead_letter WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def query_pending(
    db: aiosqlite.Connection,
    *,
    target_provider: str | None = None,
    limit: int = 50,
) -> list[dict]:
    sql = "SELECT * FROM dead_letter WHERE status = 'pending'"
    params: list = []
    if target_provider is not None:
        sql += " AND target_provider = ?"
        params.append(target_provider)
    sql += " ORDER BY created_at ASC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def update_status(db: aiosqlite.Connection, id: str, *, status: str) -> bool:
    cursor = await db.execute(
        "UPDATE dead_letter SET status = ? WHERE id = ?", (status, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def increment_retry(db: aiosqlite.Connection, id: str, *, last_retry_at: str) -> bool:
    cursor = await db.execute(
        "UPDATE dead_letter SET retry_count = retry_count + 1, last_retry_at = ? WHERE id = ?",
        (last_retry_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def count_pending(
    db: aiosqlite.Connection,
    *,
    target_provider: str | None = None,
) -> int:
    sql = "SELECT COUNT(*) FROM dead_letter WHERE status = 'pending'"
    params: list = []
    if target_provider is not None:
        sql += " AND target_provider = ?"
        params.append(target_provider)
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return int(row[0])


async def list_pending_type_age(db: aiosqlite.Connection) -> list[tuple[str, str]]:
    """Return ``(operation_type, created_at)`` for every pending item.

    Lightweight companion to :func:`count_pending` — omits the (potentially
    large) payload so a caller can apply per-operation-type age policy (e.g. the
    stuck-vs-self-healing split used for the DLQ-accumulation alert) without
    pulling full rows. Pending counts are bounded (~hundreds), same as the set
    ``expire_old`` already iterates.
    """
    cursor = await db.execute(
        "SELECT operation_type, created_at FROM dead_letter WHERE status = 'pending'"
    )
    return [(row[0], row[1]) for row in await cursor.fetchall()]


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM dead_letter WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0


async def expire_orphans_by_provider(
    db: aiosqlite.Connection,
    *,
    active_providers: list[str],
) -> list[tuple[str, str]]:
    """Expire all pending items whose target_provider is not in the active list.

    Single atomic UPDATE ... RETURNING — no pagination, no per-row round
    trips, safe for DLQs of any size. Used by the config-reload orphan
    scan to proactively clean up items targeting providers that were
    removed from the routing config.

    The reserved ``'all'`` sentinel is ALWAYS preserved: the router enqueues
    chain-exhausted items with ``target_provider='all'`` to mean "retry the
    WHOLE chain", not a single provider. ``'all'`` is never a real provider
    name, so excluding it here stops every config reload from expiring 100% of
    chain-exhausted items before the redispatch job can replay them.

    Args:
        active_providers: provider names that ARE still in the active
            config. An empty list means every real-provider pending item is an
            orphan (the ``'all'`` sentinel is still preserved).

    Returns:
        List of ``(id, target_provider)`` tuples that were expired.
    """
    if active_providers:
        placeholders = ",".join("?" * len(active_providers))
        sql = (
            "UPDATE dead_letter SET status = 'expired' "
            "WHERE status = 'pending' AND target_provider != 'all' "
            f"AND target_provider NOT IN ({placeholders}) "
            "RETURNING id, target_provider"
        )
        params: list = list(active_providers)
    else:
        sql = (
            "UPDATE dead_letter SET status = 'expired' "
            "WHERE status = 'pending' AND target_provider != 'all' "
            "RETURNING id, target_provider"
        )
        params = []
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    await db.commit()
    return [(row[0], row[1]) for row in rows]


async def query_recent(
    db: aiosqlite.Connection,
    *,
    since: str | None = None,
    target_provider: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return recent dead letter items (any status) within a time window."""
    sql = "SELECT * FROM dead_letter"
    clauses: list[str] = []
    params: list = []
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    if target_provider:
        clauses.append("target_provider = ?")
        params.append(target_provider)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def count_recent(
    db: aiosqlite.Connection,
    *,
    since: str,
    exclude_prefix: str | None = None,
) -> int:
    """Count dead-letter rows enqueued at/after ``since`` (any status).

    ``exclude_prefix`` drops operation_types starting with it — the rate-based
    provider-exhaustion storm detector passes ``chain_exhausted:judge`` so a
    self-healing 1h-TTL judge burst (worthless-late, drains itself) never counts
    toward the storm trigger. Backed by ``idx_dead_letter_created``.
    """
    sql = "SELECT COUNT(*) FROM dead_letter WHERE created_at >= ?"
    params: list = [since]
    if exclude_prefix:
        sql += " AND operation_type NOT LIKE ?"
        params.append(exclude_prefix + "%")
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def recent_optype_counts(
    db: aiosqlite.Connection,
    *,
    since: str,
    exclude_prefix: str | None = None,
) -> list[tuple[str, int]]:
    """Per-operation_type counts of dead-letters enqueued at/after ``since``.

    Feeds the storm alert's breakdown line (``light-reflection ×N``). Ordered by
    count descending. ``exclude_prefix`` behaves as in :func:`count_recent`.
    """
    sql = "SELECT operation_type, COUNT(*) AS c FROM dead_letter WHERE created_at >= ?"
    params: list = [since]
    if exclude_prefix:
        sql += " AND operation_type NOT LIKE ?"
        params.append(exclude_prefix + "%")
    sql += " GROUP BY operation_type ORDER BY c DESC"
    cursor = await db.execute(sql, params)
    return [(str(r[0]), int(r[1])) for r in await cursor.fetchall()]
