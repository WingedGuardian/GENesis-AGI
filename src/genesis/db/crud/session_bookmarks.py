"""CRUD operations for session_bookmarks table."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    cc_session_id: str,
    genesis_session_id: str = "",
    bookmark_type: str = "micro",
    topic: str = "",
    tags: str = "[]",
    transcript_path: str = "",
    created_at: str | None = None,
    source: str = "auto",
) -> bool:
    """Insert a new session bookmark.

    Returns True if inserted, False if a bookmark with the same
    (cc_session_id, source) already exists (dedup).
    """
    if created_at is None:
        created_at = datetime.now(UTC).isoformat()

    # Check-before-insert dedup: skip if same (cc_session_id, source) exists
    if cc_session_id:
        cursor = await db.execute(
            "SELECT 1 FROM session_bookmarks WHERE cc_session_id = ? AND source = ? LIMIT 1",
            (cc_session_id, source),
        )
        if await cursor.fetchone():
            return False

    await db.execute(
        """INSERT OR IGNORE INTO session_bookmarks
           (id, cc_session_id, genesis_session_id, bookmark_type,
            topic, tags, transcript_path, created_at, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, cc_session_id, genesis_session_id, bookmark_type,
         topic, tags, transcript_path, created_at, source),
    )
    await db.commit()
    return True


async def get_by_session(
    db: aiosqlite.Connection, cc_session_id: str,
) -> aiosqlite.Row | None:
    """Find bookmark for a CC session."""
    cursor = await db.execute(
        """SELECT * FROM session_bookmarks WHERE cc_session_id = ?
           ORDER BY CASE source WHEN 'explicit' THEN 0 WHEN 'plan' THEN 1 ELSE 2 END
           LIMIT 1""",
        (cc_session_id,),
    )
    return await cursor.fetchone()


async def get_by_id(
    db: aiosqlite.Connection, bookmark_id: str,
) -> aiosqlite.Row | None:
    """Find bookmark by its ID."""
    cursor = await db.execute(
        "SELECT * FROM session_bookmarks WHERE id = ?",
        (bookmark_id,),
    )
    return await cursor.fetchone()


async def get_recent(
    db: aiosqlite.Connection,
    limit: int = 10,
    source: str | None = None,
) -> list[aiosqlite.Row]:
    """Get recent bookmarks ordered by created_at descending."""
    if source is not None:
        cursor = await db.execute(
            "SELECT * FROM session_bookmarks WHERE source = ? ORDER BY created_at DESC LIMIT ?",
            (source, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM session_bookmarks ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    return await cursor.fetchall()


async def mark_enriched(
    db: aiosqlite.Connection, bookmark_id: str,
) -> None:
    """Mark a bookmark as having a rich summary."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """UPDATE session_bookmarks
           SET has_rich_summary = 1, enriched_at = ?
           WHERE id = ?""",
        (now, bookmark_id),
    )
    await db.commit()


async def increment_resumed(
    db: aiosqlite.Connection, bookmark_id: str,
) -> None:
    """Increment the resumed_count and update last_resumed_at."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """UPDATE session_bookmarks
           SET resumed_count = resumed_count + 1, last_resumed_at = ?
           WHERE id = ?""",
        (now, bookmark_id),
    )
    await db.commit()
