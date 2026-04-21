"""CRUD operations for the file_modifications audit trail.

Records which CC sessions modify which files, enabling fast diagnosis of
unexpected file changes ("what session touched this file?").
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiosqlite

logger = logging.getLogger(__name__)


async def record(
    db: aiosqlite.Connection,
    *,
    session_id: str | None,
    file_path: str,
    action: str,
    tool_name: str | None = None,
    file_hash: str | None = None,
    timestamp: str | None = None,
) -> int:
    """Insert a file modification record. Returns the row id."""
    ts = timestamp or datetime.now(UTC).isoformat()
    cursor = await db.execute(
        """INSERT INTO file_modifications
           (session_id, file_path, action, tool_name, file_hash, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, file_path, action, tool_name, file_hash, ts),
    )
    await db.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def query_by_file(
    db: aiosqlite.Connection,
    file_path: str,
    *,
    limit: int = 20,
) -> list[dict]:
    """Return recent modifications to a specific file path."""
    cursor = await db.execute(
        """SELECT * FROM file_modifications
           WHERE file_path = ?
           ORDER BY timestamp DESC
           LIMIT ?""",
        (file_path, limit),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def query_by_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> list[dict]:
    """Return all files modified by a specific session."""
    cursor = await db.execute(
        """SELECT * FROM file_modifications
           WHERE session_id = ?
           ORDER BY timestamp""",
        (session_id,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def prune_older_than(
    db: aiosqlite.Connection,
    days: int = 90,
) -> int:
    """Delete records older than N days. Returns count deleted."""
    cutoff = datetime.now(UTC).isoformat()
    # Compute cutoff by subtracting days (ISO format comparison works)
    from datetime import timedelta

    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    cursor = await db.execute(
        "DELETE FROM file_modifications WHERE timestamp < ?",
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount  # type: ignore[return-value]
