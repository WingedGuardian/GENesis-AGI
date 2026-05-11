"""CRUD operations for episodic/semantic memory metadata in SQLite.

Note: Vector embeddings live in Qdrant. This module handles the FTS5 text index
that supports hybrid search (Qdrant vectors + FTS5 + RRF fusion).
"""

from __future__ import annotations

import re

import aiosqlite


def _prepare_fts5(query: str, *, boolean: bool = False) -> str | None:
    """Prepare a query string for FTS5 MATCH.

    Args:
        query: Raw query text.
        boolean: If True, preserve FTS5 boolean operators (OR, AND) and
            parentheses. Use ONLY for queries constructed by expand_query
            (controlled vocabulary). Never for raw user input.

    Default path (boolean=False): lowercases the query to neutralize
    accidental FTS5 boolean operators — uppercase OR/AND are interpreted
    as operators by FTS5, but lowercase or/and are plain search terms.
    FTS5 content matching is case-insensitive, so lowercasing doesn't
    affect result quality.

    Returns None if the query is empty after escaping (caller should return []).
    """
    if boolean:
        # Preserve OR/AND keywords and parentheses for structured queries.
        # Strip everything else that could cause FTS5 syntax errors.
        cleaned = re.sub(r'[^\w\s()]', " ", query, flags=re.UNICODE).strip()
        # Safety: strip unbalanced parentheses rather than crash FTS5
        if cleaned.count("(") != cleaned.count(")"):
            cleaned = cleaned.replace("(", " ").replace(")", " ").strip()
    else:
        # Lowercase neutralizes accidental boolean operators (OR/AND).
        # Strip all non-alphanumeric to prevent FTS5 syntax errors.
        cleaned = re.sub(r'[^\w\s]', " ", query.lower(), flags=re.UNICODE).strip()
    return cleaned or None


async def create(
    db: aiosqlite.Connection,
    *,
    memory_id: str,
    content: str,
    source_type: str = "memory",
    tags: str = "",
    collection: str = "episodic_memory",
) -> str:
    """Insert a memory entry into the FTS5 index. Returns memory_id."""
    await db.execute(
        "INSERT INTO memory_fts (memory_id, content, source_type, tags, collection) "
        "VALUES (?, ?, ?, ?, ?)",
        (memory_id, content, source_type, tags, collection),
    )
    await db.commit()
    return memory_id


async def upsert(
    db: aiosqlite.Connection,
    *,
    memory_id: str,
    content: str,
    source_type: str = "memory",
    tags: str = "",
    collection: str = "episodic_memory",
) -> str:
    """Idempotent write: delete-then-insert for FTS5 (no ON CONFLICT support)."""
    await db.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
    await db.execute(
        "INSERT INTO memory_fts (memory_id, content, source_type, tags, collection) "
        "VALUES (?, ?, ?, ?, ?)",
        (memory_id, content, source_type, tags, collection),
    )
    await db.commit()
    return memory_id


async def find_exact_duplicate(
    db: aiosqlite.Connection,
    *,
    content: str,
) -> str | None:
    """Return memory_id if exact content already exists (any collection).

    FTS5 does not support equality (=) on content columns, so we use a
    length + substr pre-filter followed by Python exact match.

    Collection-agnostic: the FTS ``collection`` column is unreliable
    (uniformly ``episodic_memory`` regardless of actual Qdrant placement).
    """
    if not content:
        return None

    # Pre-filter: match on length and first 200 chars to narrow candidates
    prefix = content[:200]
    cursor = await db.execute(
        "SELECT memory_id, content FROM memory_fts "
        "WHERE length(content) = ? "
        "AND substr(content, 1, 200) = ? "
        "LIMIT 200",
        (len(content), prefix),
    )
    for row in await cursor.fetchall():
        if row[1] == content:
            return row[0]

    return None


async def search(
    db: aiosqlite.Connection,
    *,
    query: str,
    source_type: str | None = None,
    collection: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Full-text search on memory content. Returns matching rows."""
    escaped = _prepare_fts5(query)
    if not escaped:
        return []
    sql = "SELECT memory_id, content, source_type, collection FROM memory_fts WHERE memory_fts MATCH ?"
    params: list = [escaped]
    if source_type:
        sql += " AND source_type = ?"
        params.append(source_type)
    if collection:
        sql += " AND collection = ?"
        params.append(collection)
    sql += " LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    return [
        {"memory_id": r[0], "content": r[1], "source_type": r[2], "collection": r[3]}
        for r in rows
    ]


async def search_ranked(
    db: aiosqlite.Connection,
    *,
    query: str,
    collection: str | None = None,
    limit: int = 30,
    boolean: bool = False,
    exclude_subsystems: list[str] | None = None,
    include_only_subsystems: list[str] | None = None,
    as_of: str | None = None,
) -> list[dict]:
    """FTS5 search returning rank scores for RRF fusion.

    ``exclude_subsystems`` / ``include_only_subsystems`` filter on
    ``memory_metadata.source_subsystem``. Excludes preserve NULL
    (user-sourced) rows; includes drop them.

    The bitemporal ``invalid_at`` filter is ALWAYS applied — rows past
    their expiry never surface in recall. ``as_of`` defaults to
    ``datetime.now(UTC).isoformat()``. NULL ``invalid_at`` (= valid
    forever) always passes.
    """
    escaped = _prepare_fts5(query, boolean=boolean)
    if not escaped:
        return []

    if as_of is None:
        from datetime import UTC
        from datetime import datetime as _dt
        as_of = _dt.now(UTC).isoformat()

    # The JOIN with memory_metadata is now always required for invalid_at
    # filtering. Keeping the column-qualified SELECT format consistent.
    sql = (
        "SELECT memory_fts.memory_id, memory_fts.content, "
        "memory_fts.source_type, memory_fts.collection, memory_fts.rank "
        "FROM memory_fts LEFT JOIN memory_metadata "
        "ON memory_fts.memory_id = memory_metadata.memory_id "
        "WHERE memory_fts MATCH ?"
    )
    params: list = [escaped]
    if collection:
        sql += " AND memory_fts.collection = ?"
        params.append(collection)
    # Always-on bitemporal filter: NULL invalid_at = valid forever; otherwise
    # the fact must still be valid at as_of.
    sql += (
        " AND (memory_metadata.invalid_at IS NULL "
        "OR memory_metadata.invalid_at > ?)"
    )
    params.append(as_of)
    if exclude_subsystems:
        placeholders = ",".join("?" * len(exclude_subsystems))
        sql += (
            f" AND (memory_metadata.source_subsystem IS NULL "
            f"OR memory_metadata.source_subsystem NOT IN ({placeholders}))"
        )
        params.extend(exclude_subsystems)
    elif include_only_subsystems:
        placeholders = ",".join("?" * len(include_only_subsystems))
        sql += (
            f" AND memory_metadata.source_subsystem IN ({placeholders})"
        )
        params.extend(include_only_subsystems)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    return [
        {
            "memory_id": r[0], "content": r[1], "source_type": r[2],
            "collection": r[3], "rank": r[4],
        }
        for r in rows
    ]


async def delete(db: aiosqlite.Connection, *, memory_id: str) -> bool:
    """Delete a memory entry from the FTS5 index."""
    cursor = await db.execute(
        "DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,)
    )
    await db.commit()
    return cursor.rowcount > 0


# ── memory_metadata companion table ─────────────────────────────────


async def create_metadata(
    db: aiosqlite.Connection,
    *,
    memory_id: str,
    created_at: str,
    collection: str = "episodic_memory",
    confidence: float | None = None,
    embedding_status: str = "embedded",
    memory_class: str = "fact",
    wing: str | None = None,
    room: str | None = None,
    valid_at: str | None = None,
    invalid_at: str | None = None,
    source_subsystem: str | None = None,
) -> str:
    """Insert a row into memory_metadata. Returns memory_id.

    ``valid_at`` records when the fact became true in the real world
    (bi-temporal modeling). Defaults to ``created_at`` if not provided.
    ``invalid_at`` records when the fact stopped being true (NULL = still valid).
    ``source_subsystem`` tags writes from automated subsystems (ego,
    triage, reflection) so foreground recall can default-filter them.
    NULL = user-sourced.
    """
    resolved_valid_at = valid_at or created_at
    await db.execute(
        "INSERT OR IGNORE INTO memory_metadata "
        "(memory_id, created_at, collection, confidence, embedding_status, "
        "memory_class, wing, room, valid_at, invalid_at, source_subsystem) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (memory_id, created_at, collection, confidence, embedding_status,
         memory_class, wing, room, resolved_valid_at, invalid_at,
         source_subsystem),
    )
    await db.commit()
    return memory_id


async def invalidate_memory(
    db: aiosqlite.Connection,
    memory_id: str,
    invalid_at: str,
) -> bool:
    """Mark a memory as no longer valid (bi-temporal invalidation).

    Returns True if the memory was found and updated.
    """
    cursor = await db.execute(
        "UPDATE memory_metadata SET invalid_at = ? WHERE memory_id = ?",
        (invalid_at, memory_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete_metadata(db: aiosqlite.Connection, *, memory_id: str) -> bool:
    """Delete a memory_metadata row. Returns True if deleted."""
    cursor = await db.execute(
        "DELETE FROM memory_metadata WHERE memory_id = ?", (memory_id,)
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_by_id(db: aiosqlite.Connection, memory_id: str) -> dict | None:
    """Get a single memory by ID, joining FTS5 content with metadata."""
    cursor = await db.execute(
        "SELECT f.memory_id, f.content, f.source_type, f.tags, f.collection, "
        "       m.created_at, m.confidence, m.embedding_status, "
        "       m.valid_at, m.invalid_at "
        "FROM memory_fts f "
        "LEFT JOIN memory_metadata m ON f.memory_id = m.memory_id "
        "WHERE f.memory_id = ?",
        (memory_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "memory_id": row[0],
        "content": row[1],
        "source_type": row[2],
        "tags": row[3],
        "collection": row[4],
        "created_at": row[5],
        "confidence": row[6],
        "embedding_status": row[7],
        "valid_at": row[8],
        "invalid_at": row[9],
    }


async def list_recent(
    db: aiosqlite.Connection,
    *,
    limit: int = 50,
    offset: int = 0,
    collection: str | None = None,
) -> list[dict]:
    """List memories ordered by created_at descending (newest first)."""
    sql = (
        "SELECT f.memory_id, f.content, f.source_type, f.collection, "
        "       m.created_at, m.confidence, m.embedding_status, "
        "       m.valid_at, m.invalid_at "
        "FROM memory_metadata m "
        "JOIN memory_fts f ON f.memory_id = m.memory_id "
    )
    params: list = []
    if collection:
        sql += "WHERE m.collection = ? "
        params.append(collection)
    sql += "ORDER BY m.created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    return [
        {
            "memory_id": r[0],
            "content": r[1][:500],  # truncated for list views
            "source_type": r[2],
            "collection": r[3],
            "created_at": r[4],
            "confidence": r[5],
            "embedding_status": r[6],
            "valid_at": r[7],
            "invalid_at": r[8],
        }
        for r in rows
    ]


async def count(
    db: aiosqlite.Connection,
    *,
    collection: str | None = None,
) -> int:
    """Count memories in memory_metadata (optionally by collection)."""
    if collection:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM memory_metadata WHERE collection = ?",
            (collection,),
        )
    else:
        cursor = await db.execute("SELECT COUNT(*) FROM memory_metadata")
    row = await cursor.fetchone()
    return row[0]
