"""Knowledge base tools: recall, ingest, status."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from ..memory import mcp


def _memory_mod():
    import genesis.mcp.memory_mcp as memory_mod

    return memory_mod

logger = logging.getLogger(__name__)


@mcp.tool()
async def knowledge_recall(
    query: str,
    project: str | None = None,
    domain: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Hybrid search scoped by project/domain, authority-tagged."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._retriever is not None
    assert memory_mod._db is not None

    vector_results = await memory_mod._retriever.recall(query, source="knowledge", limit=limit)

    fts_results: list[dict] = []
    try:
        fts_results = await memory_mod.knowledge.search_fts(
            memory_mod._db, query, project=project, domain=domain, limit=limit,
        )
    except Exception:
        logger.warning("knowledge_fts search failed", exc_info=True)

    seen_ids: set[str] = set()
    merged: list[dict] = []

    for r in vector_results:
        seen_ids.add(r.memory_id)
        merged.append({
            "unit_id": r.memory_id,
            "content": r.content,
            "source": r.source,
            "score": r.score,
            "origin": "vector",
        })

    for fts_row in fts_results:
        uid = fts_row["unit_id"]
        if uid not in seen_ids:
            seen_ids.add(uid)
            merged.append({
                "unit_id": uid,
                "content": fts_row.get("body", ""),
                "concept": fts_row.get("concept", ""),
                "domain": fts_row.get("domain", ""),
                "project_type": fts_row.get("project_type", ""),
                "score": 0.0,
                "origin": "fts",
            })

    return merged[:limit]


@mcp.tool()
async def knowledge_ingest(
    content: str,
    project: str,
    domain: str,
    authority: str = "unknown",
    provenance: dict | None = None,
) -> str:
    """Store distilled knowledge unit with provenance. Returns unit ID."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._store is not None
    assert memory_mod._db is not None
    assert memory_mod._qdrant is not None

    unit_id = str(uuid.uuid4())
    now_iso = datetime.now(UTC).isoformat()
    source_doc = "manual"
    if provenance and provenance.get("source_doc"):
        source_doc = provenance["source_doc"]

    source_pipeline_val = (provenance or {}).get("source_pipeline", "recon")
    qdrant_memory_id = await memory_mod._store.store(
        content,
        f"knowledge:{project}/{domain}",
        memory_type="knowledge",
        collection="knowledge_base",
        tags=[domain, project, authority],
        confidence=0.85,
        auto_link=False,
        source_pipeline=source_pipeline_val,
    )

    embedding_model = getattr(memory_mod._store._embeddings, "model_name", "qwen3-embedding:0.6b-fp16")
    await memory_mod.knowledge.insert(
        memory_mod._db,
        id=unit_id,
        project_type=project,
        domain=domain,
        source_doc=source_doc,
        concept=content[:200],
        body=content,
        tags=f'["{domain}", "{project}", "{authority}"]',
        ingested_at=now_iso,
        qdrant_id=qdrant_memory_id,
        confidence=0.85,
        source_platform=provenance.get("platform") if provenance else None,
        section_title=provenance.get("section_title") if provenance else None,
        source_date=provenance.get("source_date") if provenance else None,
        embedding_model=embedding_model,
    )

    logger.info("Knowledge unit ingested: %s (project=%s, domain=%s)", unit_id, project, domain)
    return unit_id


@mcp.tool()
async def knowledge_status(
    project: str | None = None,
) -> dict:
    """Collection stats, staleness report, project index."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    assert memory_mod._qdrant is not None

    db_stats = await memory_mod.knowledge.stats(memory_mod._db, project=project)

    qdrant_info: dict | None = None
    try:
        qdrant_info = memory_mod.get_collection_info(memory_mod._qdrant, "knowledge_base")
    except Exception:
        logger.warning("Failed to query knowledge_base collection", exc_info=True)

    return {
        "total_units": db_stats["total"],
        "oldest_ingested": db_stats["oldest_ingested"],
        "newest_ingested": db_stats["newest_ingested"],
        "by_domain": db_stats["by_domain"],
        "qdrant_vectors": qdrant_info.get("points_count", 0) if qdrant_info else None,
    }
