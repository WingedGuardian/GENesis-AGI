"""CRUD operations for task_type_watermarks table."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite


async def get_watermark(db: aiosqlite.Connection, task_type: str) -> dict | None:
    """Fetch the watermark row for a task_type, or None if not found."""
    cursor = await db.execute(
        "SELECT * FROM task_type_watermarks WHERE task_type = ?",
        (task_type,),
    )
    row = cursor.description and await cursor.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row, strict=True))


async def upsert_watermark(
    db: aiosqlite.Connection,
    *,
    task_type: str,
    best_outcome: str,
    total_sessions: int,
    successful_sessions: int,
) -> None:
    """Create or update the watermark for a task_type.

    Only ratchets best_outcome upward (caller handles comparison).
    """
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """
        INSERT INTO task_type_watermarks
            (task_type, best_outcome, best_outcome_at, total_sessions,
             successful_sessions, last_session_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_type) DO UPDATE SET
            best_outcome = excluded.best_outcome,
            best_outcome_at = CASE
                WHEN excluded.best_outcome != task_type_watermarks.best_outcome
                THEN excluded.best_outcome_at
                ELSE task_type_watermarks.best_outcome_at
            END,
            total_sessions = excluded.total_sessions,
            successful_sessions = excluded.successful_sessions,
            last_session_at = excluded.last_session_at,
            updated_at = excluded.updated_at
        """,
        (task_type, best_outcome, now, total_sessions,
         successful_sessions, now, now),
    )
    await db.commit()
