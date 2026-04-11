"""CRUD operations for inbox_items table."""

from __future__ import annotations

import aiosqlite

# Marker prefix stored in inbox_items.error_message for rows that are parked
# in 'processing' state waiting for a user reply to the autonomous-CLI
# approval gate.  Using a constant rather than a magic string keeps the
# monitor, expire_stuck_processing, and get_awaiting_approval in sync —
# changing the prefix in one place without the others would silently break
# the resume flow.
AWAITING_APPROVAL_PREFIX = "awaiting_approval:"

# Prefix for rows whose awaiting_approval state was invalidated before the
# user replied (source file vanished, content changed, etc.).  Deliberately
# distinct from AWAITING_APPROVAL_PREFIX so SQL LIKE filters won't confuse
# invalidated-failed rows with still-awaiting rows.
APPROVAL_INVALIDATED_PREFIX = "approval_invalidated:"


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    file_path: str,
    content_hash: str,
    status: str = "pending",
    created_at: str,
    batch_id: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO inbox_items
           (id, file_path, content_hash, status, batch_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (id, file_path, content_hash, status, batch_id, created_at),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM inbox_items WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_by_file_path(db: aiosqlite.Connection, file_path: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM inbox_items WHERE file_path = ? ORDER BY created_at DESC LIMIT 1",
        (file_path,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def expire_stuck_processing(db: aiosqlite.Connection) -> int:
    """Expire items stuck in 'processing' for >2 hours to 'failed'.

    Rows carrying an ``awaiting_approval:<request_id>`` marker in
    ``error_message`` are deliberately excluded — they are not stuck, they
    are legitimately waiting for a user to respond to the autonomous-CLI
    approval gate, which can take arbitrarily long.  The inbox monitor's
    resume pass re-dispatches these rows each scan cycle until the
    approval resolves.

    Returns the number of items expired.
    """
    from datetime import UTC, datetime, timedelta

    cutoff = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    cursor = await db.execute(
        """UPDATE inbox_items
           SET status = 'failed', error_message = 'processing_timeout_expired'
           WHERE status = 'processing'
             AND created_at < ?
             AND (error_message IS NULL
                  OR error_message NOT LIKE ? || '%')""",
        (cutoff, AWAITING_APPROVAL_PREFIX),
    )
    await db.commit()
    return cursor.rowcount


async def get_awaiting_approval(db: aiosqlite.Connection) -> list[dict]:
    """Return inbox items that are parked waiting for a user approval reply.

    These are rows whose autonomous-CLI dispatch returned ``mode=blocked``
    with an ``approval_request_id`` and a reason indicating the approval
    is still pending (not rejected).  The monitor resume pass loads these
    on each scan cycle and re-dispatches them through the normal batch
    flow; the stable approval key ensures no duplicate Telegram prompts,
    and the dispatcher resolves the batch once the approval status
    changes (approved → CLI runs, rejected → row marked failed).
    """
    cursor = await db.execute(
        """SELECT id, file_path, content_hash, batch_id, error_message,
                  created_at
           FROM inbox_items
           WHERE status = 'processing'
             AND error_message LIKE ? || '%'
           ORDER BY created_at ASC""",
        (AWAITING_APPROVAL_PREFIX,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_all_known(
    db: aiosqlite.Connection, *, max_retries: int = 3,
) -> dict[str, str]:
    """Return {file_path: content_hash} for items that should NOT be reprocessed.

    Includes (blocks reprocessing):
    - pending and processing items
    - completed items whose response file still exists
    - permanently failed items (retry_count >= max_retries)

    Excludes (allows reprocessing):
    - failed items with retry_count < max_retries (retriable)
    - completed items whose response file was deleted (user wants re-eval)
    """
    from pathlib import Path

    cursor = await db.execute(
        "SELECT file_path, content_hash, status, response_path, retry_count "
        "FROM inbox_items WHERE status != 'failed'",
    )
    rows = await cursor.fetchall()
    result: dict[str, str] = {}
    for row in rows:
        file_path, content_hash, status, response_path = (
            row[0], row[1], row[2], row[3],
        )
        # If completed but response file was deleted, allow reprocessing
        if status == "completed" and response_path and not Path(response_path).exists():
            continue
        result[file_path] = content_hash

    # Permanently failed items (exhausted retries) should also block reprocessing
    cursor2 = await db.execute(
        "SELECT file_path, content_hash FROM inbox_items "
        "WHERE status = 'failed' AND retry_count >= ?",
        (max_retries,),
    )
    for row in await cursor2.fetchall():
        result[row[0]] = row[1]

    return result


async def update_status(
    db: aiosqlite.Connection,
    id: str,
    *,
    status: str,
    processed_at: str | None = None,
    error_message: str | None = None,
    evaluated_content: str | None = None,
    retry_count: int | None = None,
) -> bool:
    """Update an inbox_items row's status and related fields.

    If ``status == 'failed'`` the default behaviour is to increment
    ``retry_count`` by 1 so retry-limited scanning eventually excludes
    the file after ``max_retries`` consecutive failures.

    Pass ``retry_count=<int>`` to SET the value directly (bypassing
    the increment).  Used by the inbox resume pass on the rejection
    path to permanently block a file the user explicitly rejected —
    the retry_count is set in the SAME atomic UPDATE as the status
    change, eliminating the race window where a concurrent reader
    could see ``failed`` with ``retry_count < max_retries`` and
    re-detect the file.
    """
    if retry_count is not None:
        cursor = await db.execute(
            """UPDATE inbox_items
               SET status = ?, processed_at = ?, error_message = ?,
                   retry_count = ?
               WHERE id = ?""",
            (status, processed_at, error_message, retry_count, id),
        )
    elif status == "failed":
        # Increment retry_count on failure (default)
        cursor = await db.execute(
            """UPDATE inbox_items
               SET status = ?, processed_at = ?, error_message = ?,
                   retry_count = retry_count + 1
               WHERE id = ?""",
            (status, processed_at, error_message, id),
        )
    else:
        cursor = await db.execute(
            """UPDATE inbox_items
               SET status = ?, processed_at = ?, error_message = ?,
                   evaluated_content = COALESCE(?, evaluated_content)
               WHERE id = ?""",
            (status, processed_at, error_message, evaluated_content, id),
        )
    await db.commit()
    return cursor.rowcount > 0


async def set_batch(db: aiosqlite.Connection, id: str, *, batch_id: str) -> bool:
    cursor = await db.execute(
        "UPDATE inbox_items SET batch_id = ? WHERE id = ?",
        (batch_id, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def set_response_path(
    db: aiosqlite.Connection,
    id: str,
    *,
    response_path: str,
    processed_at: str,
    evaluated_content: str | None = None,
) -> bool:
    cursor = await db.execute(
        """UPDATE inbox_items
           SET response_path = ?, processed_at = ?, status = 'completed',
               evaluated_content = ?
           WHERE id = ?""",
        (response_path, processed_at, evaluated_content, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_evaluated_content(
    db: aiosqlite.Connection, file_path: str,
) -> str | None:
    """Return the evaluated_content from the most recent completed item for this file.

    Filters out NULL and empty-string values so callers can rely on a
    non-empty return meaning "real prior content exists."
    """
    cursor = await db.execute(
        """SELECT evaluated_content FROM inbox_items
           WHERE file_path = ? AND status = 'completed'
             AND evaluated_content IS NOT NULL
             AND evaluated_content != ''
           ORDER BY created_at DESC LIMIT 1""",
        (file_path,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def get_last_completed_at(
    db: aiosqlite.Connection, file_path: str,
) -> str | None:
    """Return the processed_at timestamp of the most recent completed evaluation.

    Used for cooldown checks — skip re-evaluation if too recent.
    Includes both normal evaluations (with response files) and Acknowledged
    items (no response file) so cooldown applies uniformly.
    """
    cursor = await db.execute(
        """SELECT processed_at FROM inbox_items
           WHERE file_path = ? AND status = 'completed'
           ORDER BY created_at DESC LIMIT 1""",
        (file_path,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def mark_url_failure(
    db: aiosqlite.Connection,
    id: str,
    *,
    response_path: str | None = None,
    processed_at: str,
    error_message: str = "partial_url_failure",
) -> bool:
    """Mark an item as failed due to unresolved URL fetch failures.

    Unlike regular failures, preserves the response_path so the user can
    still see partial evaluation results. Does NOT store evaluated_content
    so the delta logic will send full content on the next evaluation.
    """
    cursor = await db.execute(
        """UPDATE inbox_items
           SET status = 'failed', processed_at = ?, error_message = ?,
               response_path = COALESCE(?, response_path),
               evaluated_content = NULL,
               retry_count = retry_count + 1
           WHERE id = ?""",
        (processed_at, error_message, response_path, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def count_url_failures(
    db: aiosqlite.Connection,
    file_path: str,
    *,
    since_hours: int = 48,
) -> int:
    """Count recent partial_url_failure items for a file path.

    Used for retry storm prevention — stop re-evaluating files that
    persistently fail URL fetches.
    """
    from datetime import UTC, datetime, timedelta

    cutoff = (datetime.now(UTC) - timedelta(hours=since_hours)).isoformat()
    cursor = await db.execute(
        """SELECT COUNT(*) FROM inbox_items
           WHERE file_path = ? AND error_message = 'partial_url_failure'
             AND created_at > ?""",
        (file_path, cutoff),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def count_by_file_path(db: aiosqlite.Connection, file_path: str) -> int:
    """Count total inbox_items entries for a file path (all statuses).

    Used for per-file evaluation limits — prevents infinite re-evaluation
    of files that keep changing with trivial edits.
    """
    cursor = await db.execute(
        "SELECT COUNT(*) FROM inbox_items WHERE file_path = ?",
        (file_path,),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def query_pending(db: aiosqlite.Connection, *, limit: int = 50) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM inbox_items WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def query_by_batch(db: aiosqlite.Connection, batch_id: str) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM inbox_items WHERE batch_id = ? ORDER BY created_at ASC",
        (batch_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]
