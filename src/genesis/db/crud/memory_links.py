"""CRUD operations for memory_links table."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


async def batch_link_counts(
    db: aiosqlite.Connection,
    memory_ids: list[str],
) -> dict[str, tuple[int, int]]:
    """Batch-count links for multiple memory IDs.

    Returns ``{memory_id: (total_links, inbound_links)}`` where:

    * *total_links*: bidirectional count (source OR target) — same
      semantics as :func:`count_links`.
    * *inbound_links*: count of links where the memory is the **target**
      only (a measure of importance/reference frequency).

    Uses two batch queries instead of N individual ``count_links`` calls.
    Chunks at 400 IDs per batch to stay within SQLite's 999-placeholder
    limit (the bidirectional query doubles the placeholder count).
    """
    if not memory_ids:
        return {}

    _CHUNK = 400
    total_map: dict[str, int] = {}
    inbound_map: dict[str, int] = {}

    for offset in range(0, len(memory_ids), _CHUNK):
        chunk = memory_ids[offset : offset + _CHUNK]
        ph = ",".join("?" * len(chunk))

        # Bidirectional counts (replaces per-ID count_links loop)
        cursor = await db.execute(
            f"SELECT id, COUNT(*) FROM ("
            f"  SELECT source_id AS id FROM memory_links WHERE source_id IN ({ph})"
            f"  UNION ALL"
            f"  SELECT target_id AS id FROM memory_links WHERE target_id IN ({ph})"
            f") GROUP BY id",
            chunk + chunk,
        )
        for row in await cursor.fetchall():
            total_map[row[0]] = total_map.get(row[0], 0) + row[1]

        # Inbound-only counts (for graph-boosted retrieval)
        cursor = await db.execute(
            f"SELECT target_id, COUNT(*) FROM memory_links"
            f" WHERE target_id IN ({ph}) GROUP BY target_id",
            chunk,
        )
        for row in await cursor.fetchall():
            inbound_map[row[0]] = inbound_map.get(row[0], 0) + row[1]

    return {
        mid: (total_map.get(mid, 0), inbound_map.get(mid, 0))
        for mid in memory_ids
    }


async def inter_candidate_links(
    db: aiosqlite.Connection,
    memory_ids: list[str],
) -> list[tuple[str, str]]:
    """Return all directed edges ``(source, target)`` between *memory_ids*.

    Used for adjacency boost: finding which candidates in a top-K set
    link to each other. The query uses ``2 * len(memory_ids)``
    placeholders, so callers must keep the list under ~499 IDs.
    """
    if not memory_ids:
        return []
    if len(memory_ids) > 499:
        memory_ids = memory_ids[:499]
    ph = ",".join("?" * len(memory_ids))
    cursor = await db.execute(
        f"SELECT source_id, target_id FROM memory_links"
        f" WHERE source_id IN ({ph}) AND target_id IN ({ph})",
        memory_ids + memory_ids,
    )
    return [(row[0], row[1]) for row in await cursor.fetchall()]


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


async def delete_by_memory(db: aiosqlite.Connection, *, memory_id: str) -> int:
    """Delete ALL links where memory_id is source or target. Returns count deleted."""
    cursor = await db.execute(
        "DELETE FROM memory_links WHERE source_id = ? OR target_id = ?",
        (memory_id, memory_id),
    )
    await db.commit()
    return cursor.rowcount


async def prune_weak(
    db: aiosqlite.Connection,
    *,
    max_strength: float = 0.3,
    min_age_days: int = 30,
) -> int:
    """Delete weak, old links that were never reinforced.

    Criteria: strength <= *max_strength* AND created_at older than
    *min_age_days*. Returns count deleted.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=min_age_days)).isoformat()
    cursor = await db.execute(
        "DELETE FROM memory_links WHERE strength <= ? AND created_at < ?",
        (max_strength, cutoff),
    )
    await db.commit()
    pruned = cursor.rowcount
    if pruned:
        try:
            from genesis.memory.graph import invalidate_graph_cache
            invalidate_graph_cache()
        except ImportError:
            pass
    return pruned
