"""CRUD operations for pending_embeddings table."""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    memory_id: str,
    content: str,
    memory_type: str,
    collection: str,
    created_at: str,
    tags: str | None = None,
    status: str = "pending",
    source: str | None = None,
    confidence: float | None = None,
    source_session_id: str | None = None,
    transcript_path: str | None = None,
    source_line_range: str | None = None,
    extraction_timestamp: str | None = None,
    source_pipeline: str | None = None,
    source_subsystem: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO pending_embeddings
           (id, memory_id, content, memory_type, tags, collection, created_at, status,
            source, confidence, source_session_id, transcript_path,
            source_line_range, extraction_timestamp, source_pipeline,
            source_subsystem)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, memory_id, content, memory_type, tags, collection, created_at, status,
         source, confidence, source_session_id, transcript_path,
         source_line_range, extraction_timestamp, source_pipeline,
         source_subsystem),
    )
    await db.commit()
    return id


async def query_pending(
    db: aiosqlite.Connection,
    *,
    limit: int = 50,
) -> list[dict]:
    cursor = await db.execute(
        """SELECT * FROM pending_embeddings
           WHERE status = 'pending'
           ORDER BY created_at ASC
           LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def mark_embedded(
    db: aiosqlite.Connection,
    id: str,
    *,
    embedded_at: str,
) -> bool:
    cursor = await db.execute(
        """UPDATE pending_embeddings
           SET status = 'embedded', embedded_at = ?
           WHERE id = ?""",
        (embedded_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def mark_failed(
    db: aiosqlite.Connection,
    id: str,
    *,
    error_message: str,
) -> bool:
    cursor = await db.execute(
        """UPDATE pending_embeddings
           SET status = 'failed', error_message = ?
           WHERE id = ?""",
        (error_message, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def reset_failed_to_pending(
    db: aiosqlite.Connection,
    *,
    error_filter: str | None = None,
) -> int:
    """Reset failed embeddings back to pending for retry.

    If error_filter is provided, only reset items whose error_message
    contains the filter string.  Returns count of items reset.
    """
    if error_filter:
        cursor = await db.execute(
            """UPDATE pending_embeddings
               SET status = 'pending', error_message = NULL
               WHERE status = 'failed' AND error_message LIKE ?""",
            (f"%{error_filter}%",),
        )
    else:
        cursor = await db.execute(
            """UPDATE pending_embeddings
               SET status = 'pending', error_message = NULL
               WHERE status = 'failed'"""
        )
    await db.commit()
    return cursor.rowcount


async def delete_by_memory(db: aiosqlite.Connection, *, memory_id: str) -> int:
    """Delete all pending embeddings for a memory. Returns count deleted."""
    cursor = await db.execute(
        "DELETE FROM pending_embeddings WHERE memory_id = ?",
        (memory_id,),
    )
    await db.commit()
    return cursor.rowcount


async def count_pending(
    db: aiosqlite.Connection,
) -> int:
    cursor = await db.execute(
        "SELECT COUNT(*) FROM pending_embeddings WHERE status = 'pending'"
    )
    row = await cursor.fetchone()
    return int(row[0])
