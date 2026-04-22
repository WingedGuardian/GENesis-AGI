"""CRUD operations for direct_session_queue table."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

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
    timeout_s: int = 900,
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
    """Claim the oldest pending queue item. Returns None if queue is empty."""
    cursor = await db.execute(
        """SELECT * FROM direct_session_queue
           WHERE status = 'pending'
           ORDER BY created_at
           LIMIT 1""",
    )
    row = await cursor.fetchone()
    if row is None:
        return None

    row_dict = dict(row)
    now = _now()
    await db.execute(
        "UPDATE direct_session_queue SET status = 'claimed', claimed_at = ? WHERE id = ?",
        (now, row_dict["id"]),
    )
    await db.commit()
    row_dict["status"] = "claimed"
    row_dict["claimed_at"] = now
    return row_dict


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
    cutoff = datetime.now(UTC).timestamp() - max_age_s
    # SQLite datetime strings are ISO format — compare as strings
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=UTC).isoformat()
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
