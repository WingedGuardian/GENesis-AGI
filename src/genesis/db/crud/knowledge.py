"""CRUD operations for knowledge_units table + knowledge_fts index."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import aiosqlite


def _prepare_fts5(query: str) -> str | None:
    """Prepare a query string for FTS5 MATCH.

    Lowercases the query to neutralize accidental FTS5 boolean operators —
    uppercase OR/AND are interpreted as operators by FTS5, but lowercase
    or/and are plain search terms. FTS5 content matching is case-insensitive,
    so lowercasing doesn't affect result quality.

    Returns None if the query is empty after escaping (caller should return []).
    """
    cleaned = re.sub(r'[^\w\s]', " ", query.lower(), flags=re.UNICODE).strip()
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
    origin_class: str | None = None,
    _commit: bool = True,
) -> str:
    """Insert a knowledge unit into both knowledge_units and knowledge_fts. Returns id.

    Pass _commit=False for batch operations where the caller manages the transaction.
    """
    unit_id = id or str(uuid.uuid4())
    now_iso = ingested_at or datetime.now(UTC).isoformat()

    await db.execute(
        """INSERT INTO knowledge_units
           (id, project_type, domain, source_doc, source_platform, section_title,
            concept, body, relationships, caveats, tags, confidence,
            source_date, ingested_at, qdrant_id, embedding_model,
            source_pipeline, purpose, ingestion_source, origin_class)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (unit_id, project_type, domain, source_doc, source_platform, section_title,
         concept, body, relationships, caveats, tags, confidence,
         source_date, now_iso, qdrant_id, embedding_model,
         source_pipeline, purpose, ingestion_source, origin_class),
    )

    await db.execute(
        """INSERT INTO knowledge_fts
           (unit_id, concept, body, tags, domain, project_type)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (unit_id, concept, body, tags or "", domain, project_type),
    )

    if _commit:
        await db.commit()
    return unit_id


async def get(db: aiosqlite.Connection, unit_id: str) -> dict | None:
    """Get a knowledge unit by id."""
    async with db.execute(
        "SELECT * FROM knowledge_units WHERE id = ?", (unit_id,)
    ) as cursor:
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
    async with db.execute(
        "SELECT * FROM knowledge_units WHERE project_type = ? "
        "AND domain = ? AND concept = ?",
        (project_type, domain, concept),
    ) as cursor:
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
    source_pipeline: str | None = None,
    purpose: str | None = None,
    ingestion_source: str | None = None,
    origin_class: str | None = None,
    _commit: bool = True,
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

    **Caller invariant**: on re-upsert, callers should pass the existing
    row's ``id`` (via ``find_by_unique_key``) rather than a fresh UUID.
    If a different ``id`` is passed for a conflicting key, the
    ``inserted`` flag will incorrectly return True because the actual_id
    (from the pre-existing row) won't match the caller-supplied id.
    ``ingest_knowledge_unit`` enforces this correctly.
    """
    unit_id = id or str(uuid.uuid4())
    now_iso = ingested_at or datetime.now(UTC).isoformat()

    await db.execute(
        """INSERT INTO knowledge_units
           (id, project_type, domain, source_doc, source_platform, section_title,
            concept, body, relationships, caveats, tags, confidence,
            source_date, ingested_at, qdrant_id, embedding_model,
            source_pipeline, purpose, ingestion_source, origin_class)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
               embedding_model = excluded.embedding_model,
               source_pipeline = excluded.source_pipeline,
               purpose         = excluded.purpose,
               ingestion_source = excluded.ingestion_source,
               origin_class    = excluded.origin_class""",
        (unit_id, project_type, domain, source_doc, source_platform, section_title,
         concept, body, relationships, caveats, tags, confidence,
         source_date, now_iso, qdrant_id, embedding_model,
         source_pipeline, purpose, ingestion_source, origin_class),
    )

    # Find the row that actually lives in the table — either the one we
    # just inserted (same unit_id) or a pre-existing row with the same
    # unique key (different id).  This matters because callers passing
    # id=None on an UPSERT conflict would otherwise get back a brand-new
    # UUID that points at nothing.
    rows = await db.execute_fetchall(
        "SELECT id FROM knowledge_units WHERE project_type = ? "
        "AND domain = ? AND concept = ?",
        (project_type, domain, concept),
    )
    row = rows[0] if rows else None
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

    if _commit:
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
    """Full-text search on knowledge content. Returns matching rows with rank.

    JOINs back to knowledge_units to include source_pipeline (authority boosting
    in the recall merge step) plus ingested_at/confidence (reference browser
    provenance badge).
    """
    escaped = _prepare_fts5(query)
    if not escaped:
        return []
    sql = (
        "SELECT f.unit_id, f.concept, f.body, f.tags, f.domain, f.project_type,"
        " f.rank, k.source_pipeline, k.ingested_at, k.confidence,"
        " k.origin_class"
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
    rows = await db.execute_fetchall(sql, params)
    return [
        {
            "unit_id": r[0], "concept": r[1], "body": r[2],
            "tags": r[3], "domain": r[4], "project_type": r[5], "rank": r[6],
            "source_pipeline": r[7], "ingested_at": r[8], "confidence": r[9],
            # WS-3 stored provenance (0054-stamped at ingest).
            "origin_class": r[10],
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

    rows = await db.execute_fetchall(
        f"SELECT COUNT(*), MIN(ingested_at), MAX(ingested_at) FROM knowledge_units {where}",
        params,
    )
    row = rows[0] if rows else None
    total, oldest, newest = row

    # Domain breakdown
    domain_rows = await db.execute_fetchall(
        f"SELECT domain, COUNT(*) FROM knowledge_units {where} GROUP BY domain",
        params,
    )
    domains = {r[0]: r[1] for r in domain_rows}

    # Tier breakdown (curated vs recon vs other)
    tier_rows = await db.execute_fetchall(
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
    by_tier = {r[0]: r[1] for r in tier_rows}

    return {
        "total": total,
        "oldest_ingested": oldest,
        "newest_ingested": newest,
        "by_domain": domains,
        "by_tier": by_tier,
    }


async def list_by_domain(
    db: aiosqlite.Connection,
    *,
    project_type: str,
) -> dict[str, list[dict]]:
    """Return all units for a project_type grouped by domain.

    Returns ``{domain: [{id, concept, body, ingested_at, tags,
    source_pipeline, confidence}, ...]}`` sorted by domain then ingested_at
    desc. Used by the dashboard reference browser — ``source_pipeline`` and
    ``confidence`` drive the provenance badge (manual/verified vs
    auto-captured/unverified).
    """
    rows = await db.execute_fetchall(
        "SELECT id, domain, concept, body, ingested_at, tags, "
        "source_pipeline, confidence "
        "FROM knowledge_units WHERE project_type = ? "
        "ORDER BY domain, ingested_at DESC",
        (project_type,),
    )
    result: dict[str, list[dict]] = {}
    for uid, domain, concept, body, ingested_at, tags, source_pipeline, confidence in rows:
        result.setdefault(domain, []).append({
            "id": uid,
            "domain": domain,  # self-describing rows (consumers derive kind from this)
            "concept": concept,
            "body": body,
            "ingested_at": ingested_at,
            "tags": tags,
            "source_pipeline": source_pipeline,
            "confidence": confidence,
        })
    return result


async def select_fenced_intake_rows(db: aiosqlite.Connection) -> list[dict]:
    """Return intake-provenance units whose body still carries a ```json fence.

    Selection for ``scripts/cleanup_fenced_knowledge_units.py`` (2026-07-03
    context-layer audit): before the atomizer learned to unwrap fences,
    surplus-intake output was stored verbatim as fenced envelopes. Scoped to
    intake provenance so legitimate documents that merely contain inline JSON
    fences are never selected. Ordered oldest-first so a resumed cleanup run
    processes rows in a stable order.
    """
    rows = await db.execute_fetchall(
        "SELECT id, project_type, domain, source_doc, source_pipeline, "
        "body, confidence, ingested_at, qdrant_id "
        "FROM knowledge_units "
        "WHERE source_doc LIKE 'intake:%' AND body LIKE '%```json%' "
        "ORDER BY ingested_at",
    )
    keys = ("id", "project_type", "domain", "source_doc", "source_pipeline",
            "body", "confidence", "ingested_at", "qdrant_id")
    return [dict(zip(keys, row, strict=True)) for row in rows]


async def increment_retrieved_batch(
    db: aiosqlite.Connection,
    qdrant_ids: list[str],
) -> int:
    """Increment retrieved_count for knowledge units matched by Qdrant point IDs.

    Qdrant point IDs map to knowledge_units.qdrant_id (not .id) because
    ingest_knowledge_unit stores the memory_metadata ID as qdrant_id.
    Returns count of rows updated.
    """
    if not qdrant_ids:
        return 0
    placeholders = ",".join("?" for _ in qdrant_ids)
    cursor = await db.execute(
        f"UPDATE knowledge_units SET retrieved_count = retrieved_count + 1 "
        f"WHERE qdrant_id IN ({placeholders})",
        qdrant_ids,
    )
    await db.commit()
    return cursor.rowcount


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
