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


async def find_by_unique_key(
    db: aiosqlite.Connection,
    *,
    project_type: str,
    domain: str,
    concept: str,
) -> dict | None:
    """Find a knowledge unit by its unique key (project_type, domain, concept).

    Used by the reference store to detect existing entries before upsert so
    that stale Qdrant points for replaced content can be cleaned up before
    writing the new version.
    """
    cursor = await db.execute(
        "SELECT * FROM knowledge_units WHERE project_type = ? "
        "AND domain = ? AND concept = ?",
        (project_type, domain, concept),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row, strict=False))


async def upsert(
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
) -> tuple[str, bool]:
    """Insert or update a knowledge unit keyed on (project_type, domain, concept).

    Returns (unit_id, inserted) where ``inserted=True`` means a new row was
    created and ``inserted=False`` means an existing row was updated.

    On conflict (same project_type + domain + concept):
      - The existing row's ``id`` is preserved (kept stable for callers that
        track it, e.g. Qdrant ``qdrant_id`` references).
      - ``retrieved_count`` is preserved (don't reset access history).
      - Everything else is updated, including a fresh ``ingested_at``.

    The FTS5 shadow row is replaced atomically: old row deleted, new row
    inserted under the stable unit_id. aiosqlite uses deferred isolation
    (default ``isolation_level=""``), so the knowledge_units upsert, the
    conflict-resolution SELECT, the FTS5 DELETE, and the FTS5 INSERT all
    execute inside the same implicit transaction and are committed atomically
    by the single ``db.commit()`` at the end. A crash mid-sequence rolls
    back every write — no partial state.
    """
    unit_id = id or str(uuid.uuid4())
    now_iso = ingested_at or datetime.now(UTC).isoformat()

    await db.execute(
        """INSERT INTO knowledge_units
           (id, project_type, domain, source_doc, source_platform, section_title,
            concept, body, relationships, caveats, tags, confidence,
            source_date, ingested_at, qdrant_id, embedding_model)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(project_type, domain, concept) DO UPDATE SET
               source_doc      = excluded.source_doc,
               source_platform = excluded.source_platform,
               section_title   = excluded.section_title,
               body            = excluded.body,
               relationships   = excluded.relationships,
               caveats         = excluded.caveats,
               tags            = excluded.tags,
               confidence      = excluded.confidence,
               source_date     = excluded.source_date,
               ingested_at     = excluded.ingested_at,
               qdrant_id       = excluded.qdrant_id,
               embedding_model = excluded.embedding_model""",
        (unit_id, project_type, domain, source_doc, source_platform, section_title,
         concept, body, relationships, caveats, tags, confidence,
         source_date, now_iso, qdrant_id, embedding_model),
    )

    # Find the row that actually lives in the table — either the one we
    # just inserted (same unit_id) or a pre-existing row with the same
    # unique key (different id).  This matters because callers passing
    # id=None on an UPSERT conflict would otherwise get back a brand-new
    # UUID that points at nothing.
    cursor = await db.execute(
        "SELECT id FROM knowledge_units WHERE project_type = ? "
        "AND domain = ? AND concept = ?",
        (project_type, domain, concept),
    )
    row = await cursor.fetchone()
    actual_id = row[0] if row else unit_id
    inserted = actual_id == unit_id

    # Replace FTS5 shadow row so full-text search stays consistent with body.
    # FTS5 has no ON CONFLICT so we delete + re-insert under the actual id.
    await db.execute(
        "DELETE FROM knowledge_fts WHERE unit_id = ?", (actual_id,)
    )
    await db.execute(
        """INSERT INTO knowledge_fts
           (unit_id, concept, body, tags, domain, project_type)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (actual_id, concept, body, tags or "", domain, project_type),
    )

    await db.commit()
    return actual_id, inserted


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
