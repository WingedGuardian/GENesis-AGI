"""CRUD operations for direct_session_queue table."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def enqueue(
    db: aiosqlite.Connection,
    *,
    prompt: str,
    profile: str = "observe",
    model: str = "sonnet",
    effort: str = "high",
    timeout_s: int = 3600,
    notify: bool = True,
    notify_on_failure_only: bool = False,
    caller_context: str | None = None,
) -> str:
    """Insert a new queue item. Returns the queue_id."""
    queue_id = f"dsq-{uuid.uuid4().hex[:12]}"
    payload = {
        "prompt": prompt,
        "profile": profile,
        "model": model,
        "effort": effort,
        "timeout_s": timeout_s,
        "notify": notify,
        "notify_on_failure_only": notify_on_failure_only,
        "caller_context": caller_context,
    }
    await db.execute(
        """INSERT INTO direct_session_queue
           (id, payload_json, status, created_at)
           VALUES (?, ?, 'pending', ?)""",
        (queue_id, json.dumps(payload), _now()),
    )
    await db.commit()
    return queue_id


async def claim_next(db: aiosqlite.Connection) -> dict | None:
    """Atomically claim the oldest pending queue item.

    Uses UPDATE...RETURNING for a single atomic statement (SQLite 3.35+).
    Returns None if the queue is empty.
    """
    now = _now()
    cursor = await db.execute(
        """UPDATE direct_session_queue
           SET status = 'claimed', claimed_at = ?
           WHERE id = (
               SELECT id FROM direct_session_queue
               WHERE status = 'pending'
               ORDER BY created_at
               LIMIT 1
           )
           RETURNING *""",
        (now,),
    )
    row = await cursor.fetchone()
    await db.commit()
    return dict(row) if row else None


async def mark_dispatched(
    db: aiosqlite.Connection,
    queue_id: str,
    session_id: str,
) -> None:
    """Mark a queue item as dispatched with the spawned session_id."""
    await db.execute(
        """UPDATE direct_session_queue
           SET status = 'dispatched', session_id = ?, dispatched_at = ?
           WHERE id = ?""",
        (session_id, _now(), queue_id),
    )
    await db.commit()


async def mark_failed(
    db: aiosqlite.Connection,
    queue_id: str,
    error: str,
) -> None:
    """Mark a queue item as failed."""
    await db.execute(
        """UPDATE direct_session_queue
           SET status = 'failed', error_message = ?
           WHERE id = ?""",
        (error, queue_id),
    )
    await db.commit()


async def get_by_id(db: aiosqlite.Connection, queue_id: str) -> dict | None:
    """Fetch a queue item by ID."""
    cursor = await db.execute(
        "SELECT * FROM direct_session_queue WHERE id = ?", (queue_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def recover_stale_claims(
    db: aiosqlite.Connection,
    max_age_s: int = 120,
) -> int:
    """Reset claimed items older than max_age_s back to pending.

    Called on server startup to handle items claimed before a crash.
    """
    cutoff_iso = (datetime.now(UTC) - timedelta(seconds=max_age_s)).isoformat()
    cursor = await db.execute(
        """UPDATE direct_session_queue
           SET status = 'pending', claimed_at = NULL
           WHERE status = 'claimed' AND claimed_at < ?""",
        (cutoff_iso,),
    )
    await db.commit()
    return cursor.rowcount


async def count_pending(db: aiosqlite.Connection) -> int:
    """Count items in pending status."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM direct_session_queue WHERE status = 'pending'",
    )
    row = await cursor.fetchone()
    return row[0] if row else 0
