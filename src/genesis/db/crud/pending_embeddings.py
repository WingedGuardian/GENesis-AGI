"""CRUD operations for pending_embeddings table."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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

    Also flips the ``memory_metadata.embedding_status`` mirror back to
    'pending' for the same memories, in the SAME transaction. The recovery
    worker marks BOTH stores 'failed' on embed failure; re-queueing here
    without resetting the mirror would leave metadata stuck 'failed' while
    the queue row is 'pending' — a lie that _mark_superseded's Qdrant-write
    guard reads (see store.py). Orphans with no queue row are untouched
    (correctly stay 'failed' — nothing will retry them).
    """
    # Capture the memory_ids being reset BEFORE the UPDATE, to mirror them.
    if error_filter:
        id_cursor = await db.execute(
            "SELECT memory_id FROM pending_embeddings "
            "WHERE status = 'failed' AND error_message LIKE ?",
            (f"%{error_filter}%",),
        )
    else:
        id_cursor = await db.execute(
            "SELECT memory_id FROM pending_embeddings WHERE status = 'failed'"
        )
    memory_ids = [row[0] for row in await id_cursor.fetchall()]

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
    if memory_ids:
        placeholders = ",".join("?" for _ in memory_ids)
        await db.execute(
            "UPDATE memory_metadata SET embedding_status = 'pending' "  # noqa: S608
            f"WHERE embedding_status = 'failed' AND memory_id IN ({placeholders})",
            memory_ids,
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


async def purge_completed(
    db: aiosqlite.Connection,
    *,
    older_than_days: int = 30,
) -> int:
    """Delete embedded/failed rows older than *older_than_days*. Returns count deleted."""
    cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
    cursor = await db.execute(
        "DELETE FROM pending_embeddings "
        "WHERE status IN ('embedded', 'failed') AND created_at < ?",
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount


async def reconcile_orphaned_metadata(
    db: aiosqlite.Connection,
    *,
    min_age_seconds: int = 3600,
) -> int:
    """Relabel metadata orphans stuck at 'pending' with no queue row as 'failed'.

    When the recovery worker fails to embed an item it now marks BOTH the
    queue row and ``memory_metadata.embedding_status`` 'failed'. But rows
    that failed BEFORE this fix (or whose 'failed' queue row was already
    reaped by :func:`purge_completed` after 30 days) are stranded at
    'pending' in metadata with no queue row to ever retry them — no vector,
    and a lying 'pending' status that passes ``_mark_superseded``'s
    Qdrant-write guard (store.py) and drives a doomed ``update_payload``.
    Relabel them 'failed' so the mirror tells the truth.

    ``min_age_seconds`` (default 1h) spares the brief mid-``store()`` window
    between the create_metadata write and the pending_embeddings.create
    write, where a legitimately-new row transiently has metadata but not yet
    a queue row. Returns the count relabeled.
    """
    cutoff = (datetime.now(UTC) - timedelta(seconds=min_age_seconds)).isoformat()
    cursor = await db.execute(
        "UPDATE memory_metadata SET embedding_status = 'failed' "
        "WHERE embedding_status = 'pending' AND created_at < ? "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM pending_embeddings pe "
        "  WHERE pe.memory_id = memory_metadata.memory_id"
        ")",
        (cutoff,),
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
