"""CRUD operations for cc_sessions table."""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    session_type: str,
    model: str,
    started_at: str,
    last_activity_at: str,
    effort: str = "medium",
    status: str = "active",
    user_id: str | None = None,
    channel: str | None = None,
    pid: int | None = None,
    source_tag: str = "foreground",
    metadata: str | None = None,
    thread_id: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO cc_sessions
           (id, session_type, user_id, channel, model, effort, status,
            pid, started_at, last_activity_at, source_tag, metadata, thread_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, session_type, user_id, channel, model, effort, status,
         pid, started_at, last_activity_at, source_tag, metadata, thread_id),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM cc_sessions WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_active_foreground(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    channel: str,
    thread_id: str | None = None,
) -> dict | None:
    if thread_id is not None:
        cursor = await db.execute(
            """SELECT * FROM cc_sessions
               WHERE session_type = 'foreground'
                 AND status = 'active'
                 AND user_id = ?
                 AND channel = ?
                 AND thread_id = ?
               ORDER BY started_at DESC LIMIT 1""",
            (user_id, channel, thread_id),
        )
    else:
        cursor = await db.execute(
            """SELECT * FROM cc_sessions
               WHERE session_type = 'foreground'
                 AND status = 'active'
                 AND user_id = ?
                 AND channel = ?
                 AND thread_id IS NULL
               ORDER BY started_at DESC LIMIT 1""",
            (user_id, channel),
        )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def update_status(
    db: aiosqlite.Connection,
    id: str,
    *,
    status: str,
) -> bool:
    cursor = await db.execute(
        "UPDATE cc_sessions SET status = ? WHERE id = ?",
        (status, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def update_activity(
    db: aiosqlite.Connection,
    id: str,
    *,
    last_activity_at: str,
) -> bool:
    cursor = await db.execute(
        "UPDATE cc_sessions SET last_activity_at = ? WHERE id = ?",
        (last_activity_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def query_active(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM cc_sessions WHERE status = 'active' ORDER BY started_at DESC",
    )
    return [dict(r) for r in await cursor.fetchall()]


async def query_stale(
    db: aiosqlite.Connection,
    *,
    older_than: str,
) -> list[dict]:
    cursor = await db.execute(
        """SELECT * FROM cc_sessions
           WHERE status = 'active' AND last_activity_at < ?
           ORDER BY last_activity_at ASC""",
        (older_than,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def reap_stale(
    db: aiosqlite.Connection,
    *,
    older_than: str,
) -> int:
    """Mark stale active sessions as completed. Returns count reaped."""
    cursor = await db.execute(
        """UPDATE cc_sessions SET status = 'completed'
           WHERE status = 'active' AND last_activity_at < ?""",
        (older_than,),
    )
    await db.commit()
    return cursor.rowcount


async def set_pid(db: aiosqlite.Connection, id: str, pid: int) -> bool:
    """Write the subprocess PID to an existing cc_sessions row."""
    cursor = await db.execute(
        "UPDATE cc_sessions SET pid = ? WHERE id = ?", (pid, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def update_cc_session_id(
    db: aiosqlite.Connection,
    id: str,
    *,
    cc_session_id: str,
) -> bool:
    cursor = await db.execute(
        "UPDATE cc_sessions SET cc_session_id = ? WHERE id = ?",
        (cc_session_id, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def clear_cc_session_id(
    db: aiosqlite.Connection,
    id: str,
) -> bool:
    """Set cc_session_id to NULL for a given session (stale resume recovery)."""
    cursor = await db.execute(
        "UPDATE cc_sessions SET cc_session_id = NULL WHERE id = ?",
        (id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def update_model_effort(
    db: aiosqlite.Connection,
    id: str,
    *,
    model: str | None = None,
    effort: str | None = None,
) -> bool:
    """Update model and/or effort on an existing session."""
    updates = []
    params: list = []
    if model is not None:
        updates.append("model = ?")
        params.append(model)
    if effort is not None:
        updates.append("effort = ?")
        params.append(effort)
    if not updates:
        return False
    params.append(id)
    cursor = await db.execute(
        f"UPDATE cc_sessions SET {', '.join(updates)} WHERE id = ?",
        tuple(params),
    )
    await db.commit()
    return cursor.rowcount > 0


async def query_by_skill_tag(
    db: aiosqlite.Connection,
    *,
    skill_tag: str,
    status: str | None = None,
) -> list[dict]:
    """Find sessions tagged with a specific skill."""
    if status:
        cursor = await db.execute(
            """SELECT * FROM cc_sessions
               WHERE metadata LIKE ? AND status = ?
               ORDER BY started_at DESC""",
            (f'%"{skill_tag}"%', status),
        )
    else:
        cursor = await db.execute(
            """SELECT * FROM cc_sessions
               WHERE metadata LIKE ?
               ORDER BY started_at DESC""",
            (f'%"{skill_tag}"%',),
        )
    return [dict(r) for r in await cursor.fetchall()]


async def update_rate_limit(
    db: aiosqlite.Connection,
    id: str,
    *,
    rate_limited_at: str,
    rate_limit_resumes_at: str | None = None,
) -> bool:
    cursor = await db.execute(
        """UPDATE cc_sessions
           SET rate_limited_at = ?, rate_limit_resumes_at = ?
           WHERE id = ?""",
        (rate_limited_at, rate_limit_resumes_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def increment_cost(
    db: aiosqlite.Connection,
    id: str,
    *,
    cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> bool:
    """Add cost and token counts to an existing session (incremental)."""
    cursor = await db.execute(
        """UPDATE cc_sessions
           SET cost_usd = COALESCE(cost_usd, 0) + ?,
               input_tokens = COALESCE(input_tokens, 0) + ?,
               output_tokens = COALESCE(output_tokens, 0) + ?
           WHERE id = ?""",
        (cost_usd, input_tokens, output_tokens, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM cc_sessions WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0
