"""CRUD operations for memory_links table."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

logger = logging.getLogger(__name__)


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
    rows = await db.execute_fetchall(
        "SELECT * FROM memory_links WHERE source_id = ? OR target_id = ?",
        (memory_id, memory_id),
    )
    return [dict(r) for r in rows]


async def count_links(db: aiosqlite.Connection, memory_id: str) -> int:
    """Count links where memory_id is source or target."""
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM memory_links WHERE source_id = ? OR target_id = ?",
        (memory_id, memory_id),
    )
    row = rows[0] if rows else None
    return row[0] if row else 0


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
        total_rows = await db.execute_fetchall(
            f"SELECT id, COUNT(*) FROM ("
            f"  SELECT source_id AS id FROM memory_links WHERE source_id IN ({ph})"
            f"  UNION ALL"
            f"  SELECT target_id AS id FROM memory_links WHERE target_id IN ({ph})"
            f") GROUP BY id",
            chunk + chunk,
        )
        for row in total_rows:
            total_map[row[0]] = total_map.get(row[0], 0) + row[1]

        # Inbound-only counts (for graph-boosted retrieval)
        inbound_rows = await db.execute_fetchall(
            f"SELECT target_id, COUNT(*) FROM memory_links"
            f" WHERE target_id IN ({ph}) GROUP BY target_id",
            chunk,
        )
        for row in inbound_rows:
            inbound_map[row[0]] = inbound_map.get(row[0], 0) + row[1]

    return {mid: (total_map.get(mid, 0), inbound_map.get(mid, 0)) for mid in memory_ids}


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
    # DISTINCT: adjacency is a binary "are these two linked" signal. Since
    # link_type joined the PK (DLI-04), a pair with multiple types yields
    # multiple rows — collapse them so the adjacency boost isn't inflated.
    rows = await db.execute_fetchall(
        f"SELECT DISTINCT source_id, target_id FROM memory_links"
        f" WHERE source_id IN ({ph}) AND target_id IN ({ph})",
        memory_ids + memory_ids,
    )
    return [(row[0], row[1]) for row in rows]


# Seed cap for neighbors_of: the query binds 2*len(seeds) (+ optional link
# types) + 1 placeholders, so 450 keeps the worst case ~901 — under the 999
# SQLITE_MAX_VARIABLE_NUMBER floor this file's IN-list convention targets.
# (inter_candidate_links' 499 assumes its own 2n shape; don't borrow caps.)
_NEIGHBOR_SEED_CAP = 450


async def neighbors_of(
    db: aiosqlite.Connection,
    memory_ids: list[str],
    *,
    exclude: list[str] | tuple[str, ...] = (),
    limit: int = 10,
    link_types: tuple[str, ...] | None = None,
) -> list[dict]:
    """1-hop neighbors of *memory_ids* via ``memory_links``, strongest first.

    Both directions are followed and collapsed to ONE row per neighbor with
    ``MAX(strength)`` — the PK is ``(source_id, target_id, link_type)`` (DLI-04),
    so a pair with multiple link types yields multiple rows, and several seeds
    can reach the same neighbor; a naive UNION would burn ``limit`` slots on
    duplicates. Ties break on neighbor id (deterministic across runs). Seeds
    and ``exclude`` ids are never returned: exclusion happens in PYTHON, not
    SQL, so (a) the placeholder count stays bounded regardless of exclude
    size, and (b) capping an oversized seed list can never re-admit a
    truncated seed as a "neighbor".

    ``link_types`` optionally restricts which edge types are followed —
    callers expanding into LLM-visible context should exclude adversarial
    types like ``contradicts`` (the LongMemEval graph arm only ever stores
    supports/extends, so it passes None).

    Returns ``[{"memory_id": ..., "strength": ...}]``. Used by recall-time
    graph expansion (LongMemEval graph arm; intended canonical home for the
    committed prod graph/entity recall wiring).
    """
    if not memory_ids or limit <= 0:
        return []
    seeds = memory_ids
    if len(seeds) > _NEIGHBOR_SEED_CAP:
        logger.warning(
            "neighbors_of: %d seed ids truncated to %d (placeholder budget)",
            len(seeds),
            _NEIGHBOR_SEED_CAP,
        )
        seeds = seeds[:_NEIGHBOR_SEED_CAP]
    # Drop the FULL original seed list (not just the capped slice) + exclude.
    dropped = {*memory_ids, *exclude}
    ph = ",".join("?" * len(seeds))
    type_clause = ""
    type_params: list[str] = []
    if link_types:
        type_ph = ",".join("?" * len(link_types))
        type_clause = f" AND link_type IN ({type_ph})"
        type_params = list(link_types)
    rows = await db.execute_fetchall(
        f"SELECT neighbor, MAX(strength) AS s FROM ("
        f"  SELECT target_id AS neighbor, strength FROM memory_links"
        f"    WHERE source_id IN ({ph}){type_clause}"
        f"  UNION ALL"
        f"  SELECT source_id AS neighbor, strength FROM memory_links"
        f"    WHERE target_id IN ({ph}){type_clause}"
        f") GROUP BY neighbor ORDER BY s DESC, neighbor LIMIT ?",
        [*seeds, *type_params, *seeds, *type_params, limit + len(dropped)],
    )
    out = [{"memory_id": row[0], "strength": row[1]} for row in rows if row[0] not in dropped]
    return out[:limit]


async def get_bidirectional(db: aiosqlite.Connection, memory_id: str) -> list[dict]:
    """Get all links where memory_id is source or target (undirected query)."""
    return await get_links_for(db, memory_id)


async def delete(
    db: aiosqlite.Connection,
    *,
    source_id: str,
    target_id: str,
    link_type: str | None = None,
) -> bool:
    """Delete memory link(s) between a pair. Returns True if any row deleted.

    With ``link_type`` set, deletes only that typed link. Without it, deletes
    ALL link types between the pair — be deliberate, since ``link_type`` joined
    the PK (DLI-04) a pair can now hold several edges and the untyped form
    removes every one of them.
    """
    if link_type is not None:
        cursor = await db.execute(
            "DELETE FROM memory_links WHERE source_id = ? AND target_id = ? AND link_type = ?",
            (source_id, target_id, link_type),
        )
    else:
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
