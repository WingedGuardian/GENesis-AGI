"""Shared knowledge-unit ingestion helper.

Used by both the ``knowledge_ingest`` / ``reference_store`` MCP tools in
``genesis.mcp.memory.knowledge`` and the automatic reference extractor in
``genesis.memory.extraction_job``. Handles upsert semantics, Qdrant point
replacement, FTS5 index consistency, and memory_class override.

This lives in ``memory/`` (not ``mcp/memory/``) so extraction_job.py can
depend on it without an upward layer dependency.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.db.crud import knowledge as knowledge_crud

if TYPE_CHECKING:
    import aiosqlite

    from genesis.memory.store import MemoryStore

logger = logging.getLogger(__name__)


async def ingest_knowledge_unit(
    *,
    store: MemoryStore,
    db: aiosqlite.Connection,
    content: str,
    project: str,
    domain: str,
    authority: str = "unknown",
    provenance: dict | None = None,
    memory_class: str | None = None,
    concept: str | None = None,
    tags_json: str | None = None,
    force_fts5_only: bool = False,
    purpose: list[str] | None = None,
    ingestion_source: str | None = None,
) -> str:
    """Ingest or upsert a knowledge unit, returning the stable unit_id.

    Behaviors:
    - Idempotent on ``(project, domain, concept)``. Re-ingesting the same
      logical entry updates in place and preserves the original unit_id.
    - Writes a Qdrant point via ``store.store()`` (synchronous dedup via
      ``find_exact_duplicate``). If the row previously pointed at a
      different Qdrant ID, the stale point is deleted via ``store.delete()``.
    - Writes the knowledge_units + knowledge_fts pair via ``knowledge.upsert``
      inside a single deferred transaction.

    ``memory_class`` overrides the activation-weight classification
    downstream. Pass ``"fact"`` to avoid the 0.7x penalty that
    ``classify_memory`` applies to URL-bearing content.

    ``concept`` overrides the default ``content[:200]`` derivation. The
    reference store uses a structured identifier like
    ``"ScarletAndRage forum login"``.

    ``tags_json`` overrides the default tags array. Pass a pre-built JSON
    string when the caller needs reference-specific tag layout.
    """
    resolved_concept = concept if concept is not None else content[:200]
    now_iso = datetime.now(UTC).isoformat()
    source_doc = "manual"
    if provenance and provenance.get("source_doc"):
        source_doc = provenance["source_doc"]

    existing = await knowledge_crud.find_by_unique_key(
        db, project_type=project, domain=domain, concept=resolved_concept,
    )
    unit_id = existing["id"] if existing else str(uuid.uuid4())
    old_qdrant_id = existing.get("qdrant_id") if existing else None

    source_pipeline_val = (provenance or {}).get("source_pipeline", "knowledge_ingest")
    source_session_id = (provenance or {}).get("session_id")
    qdrant_memory_id = await store.store(
        content,
        f"knowledge:{project}/{domain}",
        memory_type="knowledge",
        collection="knowledge_base",
        tags=[domain, project, authority],
        confidence=0.85,
        auto_link=False,
        source_pipeline=source_pipeline_val,
        memory_class=memory_class,
        source_session_id=source_session_id,
        force_fts5_only=force_fts5_only,
    )

    # If we replaced an existing entry whose Qdrant point ID differs from
    # the new one, the old point is now orphaned — delete it so Qdrant
    # stays in sync with the knowledge_units row of record. store.delete()
    # handles metadata, FTS5, Qdrant, links, and pending_embeddings cascade.
    if old_qdrant_id and old_qdrant_id != qdrant_memory_id:
        try:
            await store.delete(old_qdrant_id)
        except Exception:
            logger.error(
                "Failed to clean up stale Qdrant point %s while upserting "
                "knowledge unit %s", old_qdrant_id, unit_id, exc_info=True,
            )

    embedding_model = getattr(store._embeddings, "model_name", "qwen3-embedding:0.6b-fp16")
    resolved_tags = tags_json or json.dumps([domain, project, authority])
    actual_id, inserted = await knowledge_crud.upsert(
        db,
        id=unit_id,
        project_type=project,
        domain=domain,
        source_doc=source_doc,
        concept=resolved_concept,
        body=content,
        tags=resolved_tags,
        ingested_at=now_iso,
        qdrant_id=qdrant_memory_id,
        confidence=0.85,
        source_platform=provenance.get("platform") if provenance else None,
        section_title=provenance.get("section_title") if provenance else None,
        source_date=provenance.get("source_date") if provenance else None,
        embedding_model=embedding_model,
        source_pipeline=provenance.get("source_pipeline") if provenance else None,
        purpose=json.dumps(purpose) if purpose else None,
        ingestion_source=ingestion_source,
    )

    logger.info(
        "Knowledge unit %s (project=%s, domain=%s)",
        "ingested" if inserted else "updated",
        project, domain,
    )
    return actual_id
