"""CRUD operations for episodic/semantic memory metadata in SQLite.

Note: Vector embeddings live in Qdrant. This module handles the FTS5 text index
that supports hybrid search (Qdrant vectors + FTS5 + RRF fusion).
"""

from __future__ import annotations

import re

import aiosqlite


def _escape_fts5(query: str) -> str | None:
    """Escape FTS5 special characters to prevent syntax errors from user input.

    Returns None if the query is empty after escaping (caller should return []).
    """
    cleaned = re.sub(r'[^\w\s]', " ", query, flags=re.UNICODE).strip()
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
    escaped = _escape_fts5(query)
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
) -> list[dict]:
    """FTS5 search returning rank scores for RRF fusion."""
    escaped = _escape_fts5(query)
    if not escaped:
        return []
    sql = (
        "SELECT memory_id, content, source_type, collection, rank "
        "FROM memory_fts WHERE memory_fts MATCH ?"
    )
    params: list = [escaped]
    if collection:
        sql += " AND collection = ?"
        params.append(collection)
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
