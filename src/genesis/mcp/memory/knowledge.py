"""Knowledge base tools: recall, ingest, status."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from ..memory import mcp


def _memory_mod():
    import genesis.mcp.memory_mcp as memory_mod

    return memory_mod

logger = logging.getLogger(__name__)

# Authority tier multipliers for retrieval ranking.
# Curated (user-directed) content outranks auto-discovered recon noise.
_AUTHORITY_BOOST: dict[str, float] = {
    "curated": 1.5,
    "conversation": 1.0,
    "recon": 0.5,
}


def _apply_authority_boost(merged: list[dict]) -> list[dict]:
    """Apply authority-tier score multiplier and sort by boosted score."""
    for item in merged:
        pipeline = item.get("source_pipeline") or ""
        boost = 1.0
        for tier, multiplier in _AUTHORITY_BOOST.items():
            if tier in pipeline:
                boost = multiplier
                break
        item["score"] = item.get("score", 0.0) * boost
    merged.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    return merged


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
            "source_pipeline": r.source_pipeline,
        })

    for idx, fts_row in enumerate(fts_results):
        uid = fts_row["unit_id"]
        if uid not in seen_ids:
            seen_ids.add(uid)
            # Normalize FTS rank into a 0-1 score so FTS results compete
            # with vector results. FTS5 rank is negative (closer to 0 = better).
            # Map rank position to score: first result ~0.8, decaying linearly.
            fts_score = max(0.1, 0.8 - (idx * 0.05))
            merged.append({
                "unit_id": uid,
                "content": fts_row.get("body", ""),
                "concept": fts_row.get("concept", ""),
                "domain": fts_row.get("domain", ""),
                "project_type": fts_row.get("project_type", ""),
                "score": fts_score,
                "origin": "fts",
                "source_pipeline": fts_row.get("source_pipeline"),
            })

    return _apply_authority_boost(merged)[:limit]


@mcp.tool()
async def knowledge_ingest(
    content: str,
    project: str,
    domain: str,
    authority: str = "unknown",
    provenance: dict | None = None,
    purpose: list[str] | None = None,
    ingestion_source: str | None = None,
) -> str:
    """Store distilled knowledge unit with provenance. Returns unit ID.

    Args:
        content: The knowledge content to store.
        project: Project classification (e.g., "professional", "cloud-eng").
        domain: Knowledge domain (e.g., "aws", "resume-advice").
        authority: Authority level (e.g., "curated", "unknown").
        provenance: Optional dict with source_doc, source_pipeline, platform, etc.
        purpose: Optional list of purpose tags (e.g., ["resume-prep", "cloud-eng"]).
        ingestion_source: Original file path or URL for full provenance tracking.
    """
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

    purpose_json = json.dumps(purpose) if purpose else None
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
        source_pipeline=source_pipeline_val,
        purpose=purpose_json,
        ingestion_source=ingestion_source,
    )

    logger.info("Knowledge unit ingested: %s (project=%s, domain=%s, pipeline=%s)",
                unit_id, project, domain, source_pipeline_val)
    return unit_id


@mcp.tool()
async def knowledge_status(
    project: str | None = None,
) -> dict:
    """Collection stats, staleness report, project index with tier breakdown."""
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
        "by_tier": db_stats.get("by_tier", {}),
        "qdrant_vectors": qdrant_info.get("points_count", 0) if qdrant_info else None,
    }


def _get_orchestrator():
    """Lazily build the KnowledgeOrchestrator using the live runtime."""
    from genesis.knowledge.distillation import DistillationPipeline
    from genesis.knowledge.manifest import ManifestManager
    from genesis.knowledge.orchestrator import KnowledgeOrchestrator
    from genesis.knowledge.processors.registry import build_default_registry
    from genesis.runtime._core import GenesisRuntime

    rt = GenesisRuntime.instance()
    if rt._router is None:
        raise RuntimeError("Router not available — Genesis not fully bootstrapped")

    return KnowledgeOrchestrator(
        registry=build_default_registry(),
        distillation=DistillationPipeline(router=rt._router),
        manifest=ManifestManager(),
    )


@mcp.tool()
async def knowledge_ingest_source(
    source: str,
    project_type: str,
    domain: str = "auto",
    purpose: list[str] | None = None,
) -> dict:
    """Ingest a file or URL into the knowledge base via the full pipeline.

    Detects source type, extracts content, distills into knowledge units,
    and stores with curated authority tier.

    Args:
        source: File path or URL to ingest.
        project_type: Project classification (e.g., "professional", "cloud-eng").
        domain: Knowledge domain (default "auto" — LLM determines).
        purpose: Optional purpose tags (e.g., ["resume-prep"]).
    """
    orchestrator = _get_orchestrator()
    result = await orchestrator.ingest_source(
        source, project_type=project_type, domain=domain, purpose=purpose,
    )
    return {
        "source": result.source,
        "source_type": result.source_type,
        "units_created": result.units_created,
        "unit_ids": result.unit_ids,
        "quality_flags": result.quality_flags,
        "error": result.error,
    }


@mcp.tool()
async def knowledge_ingest_batch(
    directory: str,
    project_type: str,
    domain: str = "auto",
    purpose: list[str] | None = None,
    extensions: list[str] | None = None,
) -> dict:
    """Batch-ingest all supported files from a directory.

    Args:
        directory: Path to directory containing files to ingest.
        project_type: Project classification for all ingested content.
        domain: Knowledge domain (default "auto").
        purpose: Optional purpose tags applied to all units.
        extensions: Optional filter — only process files with these extensions.
    """
    orchestrator = _get_orchestrator()
    results = await orchestrator.ingest_batch(
        directory, project_type=project_type, domain=domain,
        purpose=purpose, extensions=extensions,
    )
    return {
        "total_sources": len(results),
        "total_units": sum(r.units_created for r in results),
        "results": [
            {
                "source": r.source,
                "source_type": r.source_type,
                "units_created": r.units_created,
                "quality_flags": r.quality_flags,
                "error": r.error,
            }
            for r in results
        ],
    }


@mcp.tool()
async def resume_review(
    resume_source: str,
    job_description: str | None = None,
    knowledge_domains: list[str] | None = None,
) -> dict:
    """Two-pass resume review: native LLM analysis + knowledge-augmented critique.

    Pass 1 evaluates structure, clarity, impact, ATS compatibility.
    Pass 2 queries the knowledge base for professional context and
    domain knowledge to provide grounded, non-generic feedback.

    Args:
        resume_source: Path to resume file (PDF or text) or raw resume text.
        job_description: Optional job description text or URL for alignment scoring.
        knowledge_domains: Optional list of KB domains to query (default: all).
    """
    from pathlib import Path

    from genesis.knowledge.applications.resume_review import ResumeReviewer
    from genesis.runtime._core import GenesisRuntime

    rt = GenesisRuntime.instance()
    if rt._router is None:
        return {"error": "Router not available"}

    # Resolve resume text
    resume_text = resume_source
    source_path = Path(resume_source)
    if source_path.exists():
        if source_path.suffix.lower() == ".pdf":
            import pymupdf

            doc = pymupdf.open(str(source_path))
            resume_text = "\n\n".join(
                page.get_text().strip() for page in doc if page.get_text().strip()
            )
            doc.close()
        else:
            resume_text = source_path.read_text(encoding="utf-8", errors="replace")

    reviewer = ResumeReviewer(router=rt._router)
    result = await reviewer.review(
        resume_text,
        job_description=job_description,
        knowledge_domains=knowledge_domains,
    )

    return {
        "pass1_analysis": result.pass1_analysis,
        "pass2_augmented": result.pass2_augmented,
        "combined_output": result.combined_output,
        "knowledge_citations": result.knowledge_citations,
        "error": result.error,
    }
