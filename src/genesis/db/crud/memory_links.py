"""CRUD operations for memory_links table."""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    source_id: str,
    target_id: str,
    link_type: str,
    strength: float = 0.5,
    created_at: str,
) -> tuple[str, str]:
    """Insert a memory link. Returns (source_id, target_id)."""
    await db.execute(
        "INSERT INTO memory_links (source_id, target_id, link_type, strength, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_id, target_id, link_type, strength, created_at),
    )
    await db.commit()
    return (source_id, target_id)


async def get_links_for(db: aiosqlite.Connection, memory_id: str) -> list[dict]:
    """Get all links where memory_id is source or target."""
    cursor = await db.execute(
        "SELECT * FROM memory_links WHERE source_id = ? OR target_id = ?",
        (memory_id, memory_id),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def count_links(db: aiosqlite.Connection, memory_id: str) -> int:
    """Count links where memory_id is source or target."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM memory_links WHERE source_id = ? OR target_id = ?",
        (memory_id, memory_id),
    )
    row = await cursor.fetchone()
    return row[0]


async def get_bidirectional(db: aiosqlite.Connection, memory_id: str) -> list[dict]:
    """Get all links where memory_id is source or target (undirected query)."""
    return await get_links_for(db, memory_id)


async def delete(
    db: aiosqlite.Connection,
    *,
    source_id: str,
    target_id: str,
) -> bool:
    """Delete a memory link. Returns True if deleted."""
    cursor = await db.execute(
        "DELETE FROM memory_links WHERE source_id = ? AND target_id = ?",
        (source_id, target_id),
    )
    await db.commit()
    return cursor.rowcount > 0
