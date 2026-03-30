"""CRUD operations for knowledge_units table + knowledge_fts index."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import aiosqlite


def _escape_fts5(query: str) -> str | None:
    """Escape FTS5 special characters to prevent syntax errors from user input.

    Returns None if the query is empty after escaping (caller should return []).
    """
    cleaned = re.sub(r'[^\w\s]', " ", query, flags=re.UNICODE).strip()
    return cleaned or None


async def insert(
    db: aiosqlite.Connection,
    *,
    project_type: str,
    domain: str,
    source_doc: str,
    concept: str,
    body: str,
    id: str | None = None,
    source_platform: str | None = None,
    section_title: str | None = None,
    relationships: str | None = None,
    caveats: str | None = None,
    tags: str | None = None,
    confidence: float = 0.85,
    source_date: str | None = None,
    ingested_at: str | None = None,
    qdrant_id: str | None = None,
    embedding_model: str | None = None,
) -> str:
    """Insert a knowledge unit into both knowledge_units and knowledge_fts. Returns id."""
    unit_id = id or str(uuid.uuid4())
    now_iso = ingested_at or datetime.now(UTC).isoformat()

    await db.execute(
        """INSERT INTO knowledge_units
           (id, project_type, domain, source_doc, source_platform, section_title,
            concept, body, relationships, caveats, tags, confidence,
            source_date, ingested_at, qdrant_id, embedding_model)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (unit_id, project_type, domain, source_doc, source_platform, section_title,
         concept, body, relationships, caveats, tags, confidence,
         source_date, now_iso, qdrant_id, embedding_model),
    )

    await db.execute(
        """INSERT INTO knowledge_fts
           (unit_id, concept, body, tags, domain, project_type)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (unit_id, concept, body, tags or "", domain, project_type),
    )

    await db.commit()
    return unit_id


async def get(db: aiosqlite.Connection, unit_id: str) -> dict | None:
    """Get a knowledge unit by id."""
    cursor = await db.execute(
        "SELECT * FROM knowledge_units WHERE id = ?", (unit_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row, strict=False))


async def search_fts(
    db: aiosqlite.Connection,
    query: str,
    *,
    project: str | None = None,
    domain: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Full-text search on knowledge content. Returns matching rows with rank."""
    escaped = _escape_fts5(query)
    if not escaped:
        return []
    sql = (
        "SELECT unit_id, concept, body, tags, domain, project_type, rank "
        "FROM knowledge_fts WHERE knowledge_fts MATCH ?"
    )
    params: list = [escaped]
    if project:
        sql += " AND project_type = ?"
        params.append(project)
    if domain:
        sql += " AND domain = ?"
        params.append(domain)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    return [
        {
            "unit_id": r[0], "concept": r[1], "body": r[2],
            "tags": r[3], "domain": r[4], "project_type": r[5], "rank": r[6],
        }
        for r in rows
    ]


async def stats(
    db: aiosqlite.Connection,
    *,
    project: str | None = None,
) -> dict:
    """Aggregate stats for knowledge units."""
    if project:
        cursor = await db.execute(
            """SELECT COUNT(*), MIN(ingested_at), MAX(ingested_at)
               FROM knowledge_units WHERE project_type = ?""",
            (project,),
        )
    else:
        cursor = await db.execute(
            "SELECT COUNT(*), MIN(ingested_at), MAX(ingested_at) FROM knowledge_units"
        )
    row = await cursor.fetchone()
    total, oldest, newest = row

    # Domain breakdown
    if project:
        cursor = await db.execute(
            "SELECT domain, COUNT(*) FROM knowledge_units WHERE project_type = ? GROUP BY domain",
            (project,),
        )
    else:
        cursor = await db.execute(
            "SELECT domain, COUNT(*) FROM knowledge_units GROUP BY domain"
        )
    domains = {r[0]: r[1] for r in await cursor.fetchall()}

    return {
        "total": total,
        "oldest_ingested": oldest,
        "newest_ingested": newest,
        "by_domain": domains,
    }


async def delete(db: aiosqlite.Connection, unit_id: str) -> bool:
    """Delete a knowledge unit from both knowledge_units and knowledge_fts."""
    cursor = await db.execute(
        "DELETE FROM knowledge_units WHERE id = ?", (unit_id,)
    )
    await db.execute(
        "DELETE FROM knowledge_fts WHERE unit_id = ?", (unit_id,)
    )
    await db.commit()
    return cursor.rowcount > 0
