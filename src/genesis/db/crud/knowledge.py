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
    source_pipeline: str | None = None,
    purpose: str | None = None,
    ingestion_source: str | None = None,
) -> str:
    """Insert a knowledge unit into both knowledge_units and knowledge_fts. Returns id."""
    unit_id = id or str(uuid.uuid4())
    now_iso = ingested_at or datetime.now(UTC).isoformat()

    await db.execute(
        """INSERT INTO knowledge_units
           (id, project_type, domain, source_doc, source_platform, section_title,
            concept, body, relationships, caveats, tags, confidence,
            source_date, ingested_at, qdrant_id, embedding_model,
            source_pipeline, purpose, ingestion_source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (unit_id, project_type, domain, source_doc, source_platform, section_title,
         concept, body, relationships, caveats, tags, confidence,
         source_date, now_iso, qdrant_id, embedding_model,
         source_pipeline, purpose, ingestion_source),
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
    """Full-text search on knowledge content. Returns matching rows with rank.

    JOINs back to knowledge_units to include source_pipeline for authority
    boosting in the recall merge step.
    """
    escaped = _escape_fts5(query)
    if not escaped:
        return []
    sql = (
        "SELECT f.unit_id, f.concept, f.body, f.tags, f.domain, f.project_type,"
        " f.rank, k.source_pipeline"
        " FROM knowledge_fts f"
        " LEFT JOIN knowledge_units k ON k.id = f.unit_id"
        " WHERE knowledge_fts MATCH ?"
    )
    params: list = [escaped]
    if project:
        sql += " AND f.project_type = ?"
        params.append(project)
    if domain:
        sql += " AND f.domain = ?"
        params.append(domain)
    sql += " ORDER BY f.rank LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    return [
        {
            "unit_id": r[0], "concept": r[1], "body": r[2],
            "tags": r[3], "domain": r[4], "project_type": r[5], "rank": r[6],
            "source_pipeline": r[7],
        }
        for r in rows
    ]


async def stats(
    db: aiosqlite.Connection,
    *,
    project: str | None = None,
) -> dict:
    """Aggregate stats for knowledge units."""
    where = "WHERE project_type = ?" if project else ""
    params: tuple = (project,) if project else ()

    cursor = await db.execute(
        f"SELECT COUNT(*), MIN(ingested_at), MAX(ingested_at) FROM knowledge_units {where}",
        params,
    )
    row = await cursor.fetchone()
    total, oldest, newest = row

    # Domain breakdown
    cursor = await db.execute(
        f"SELECT domain, COUNT(*) FROM knowledge_units {where} GROUP BY domain",
        params,
    )
    domains = {r[0]: r[1] for r in await cursor.fetchall()}

    # Tier breakdown (curated vs recon vs other)
    cursor = await db.execute(
        f"""SELECT
                CASE
                    WHEN source_pipeline = 'curated' THEN 'curated'
                    WHEN source_pipeline = 'recon' THEN 'recon'
                    WHEN source_pipeline IS NULL THEN 'unknown'
                    ELSE source_pipeline
                END AS tier,
                COUNT(*)
            FROM knowledge_units {where}
            GROUP BY tier""",
        params,
    )
    by_tier = {r[0]: r[1] for r in await cursor.fetchall()}

    return {
        "total": total,
        "oldest_ingested": oldest,
        "newest_ingested": newest,
        "by_domain": domains,
        "by_tier": by_tier,
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
