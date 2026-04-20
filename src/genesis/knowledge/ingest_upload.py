"""Ingestion worker for dashboard uploads.

Thin bridge: updates DB status → calls KnowledgeOrchestrator.ingest_source →
updates DB status with results. Called via run_coroutine_threadsafe from the
upload route handler.

Supports two modes:
- "extract": Full distillation pipeline (chunk → LLM → knowledge units)
- "store": Store file content as a single knowledge unit (no LLM, instant)
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_COMPLETED_DIR = Path.home() / ".genesis" / "knowledge" / "completed"


async def run_ingest(
    upload_id: str,
    *,
    project_type: str,
    domain: str = "auto",
    purpose: list[str] | None = None,
    context: str = "",
    mode: str = "extract",
) -> None:
    """Run the ingestion pipeline for an uploaded file.

    Updates the knowledge_uploads row with results on completion or failure.
    Moves the file to the completed directory on success.
    """
    from genesis.db.crud import knowledge_uploads
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if rt.db is None:
        logger.error("DB not available for ingest upload %s", upload_id)
        return

    # Fetch upload record to get file_path
    upload = await knowledge_uploads.get(rt.db, upload_id)
    if upload is None:
        logger.error("Upload %s not found in DB", upload_id)
        return

    file_path = upload["file_path"]
    filename = upload["filename"]

    try:
        if mode == "store":
            unit_ids = await _store_as_is(
                file_path, filename,
                project_type=project_type,
                domain=domain,
                purpose=purpose,
                context=context,
            )
        else:
            unit_ids = await _extract_with_pipeline(
                upload_id, file_path,
                project_type=project_type,
                domain=domain,
                purpose=purpose,
                context=context,
            )

        # Move file to completed directory
        _move_to_completed(file_path, upload_id)

        await knowledge_uploads.update_status(
            rt.db, upload_id,
            status="completed",
            unit_ids=unit_ids,
        )

        logger.info(
            "Ingest completed for %s: %d units created (mode=%s)",
            filename, len(unit_ids), mode,
        )

    except Exception as exc:
        logger.exception("Ingest worker failed for upload %s", upload_id)
        try:
            await knowledge_uploads.update_status(
                rt.db, upload_id,
                status="failed",
                error_message=str(exc) or "Internal error during ingestion",
            )
        except Exception:
            logger.exception("Failed to update upload status after error")


async def _store_as_is(
    file_path: str,
    filename: str,
    *,
    project_type: str,
    domain: str,
    purpose: list[str] | None,
    context: str,
) -> list[str]:
    """Store file content as a single knowledge unit — no LLM, instant."""
    import uuid
    from datetime import UTC, datetime

    import genesis.mcp.memory_mcp as memory_mod

    memory_mod._require_init()
    assert memory_mod._store is not None
    assert memory_mod._db is not None

    from genesis.db.crud import knowledge as knowledge_crud

    # Read file content (cap at 2MB for single-unit storage)
    path = Path(file_path)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError(
            "File too large for store-as-is mode (>2MB). "
            "Use 'Extract & distill' for large documents."
        )
    file_content = path.read_text(encoding="utf-8", errors="replace")

    # Use context or filename as concept
    concept = context[:200] if context else filename

    # Determine domain
    effective_domain = domain if domain != "auto" else "general"

    unit_id = str(uuid.uuid4())
    now_iso = datetime.now(UTC).isoformat()
    purpose_json = json.dumps(purpose) if purpose else None
    embedding_model = getattr(
        memory_mod._store._embeddings, "model_name", "unknown"
    )

    # Build tags
    tags = [effective_domain, project_type, "stored-as-is"]
    if context:
        tags.append("user-context")

    # Store to Qdrant
    qdrant_id = await memory_mod._store.store(
        file_content,
        f"knowledge:{project_type}/{effective_domain}",
        memory_type="knowledge",
        collection="knowledge_base",
        tags=tags,
        confidence=0.95,
        auto_link=False,
        source_pipeline="curated",
    )

    # Store to SQLite
    await knowledge_crud.insert(
        memory_mod._db,
        id=unit_id,
        project_type=project_type,
        domain=effective_domain,
        source_doc=file_path,
        concept=concept,
        body=file_content,
        relationships=None,
        caveats=json.dumps([f"User context: {context}"]) if context else None,
        tags=json.dumps(tags),
        confidence=0.95,
        ingested_at=now_iso,
        qdrant_id=qdrant_id,
        section_title=None,
        source_date=None,
        embedding_model=embedding_model,
        source_pipeline="curated",
        purpose=purpose_json,
        ingestion_source=file_path,
        _commit=True,
    )

    logger.info("Stored %s as single unit %s (store-as-is mode)", filename, unit_id)
    return [unit_id]


async def _extract_with_pipeline(
    upload_id: str,
    file_path: str,
    *,
    project_type: str,
    domain: str,
    purpose: list[str] | None,
    context: str,
) -> list[str]:
    """Run the full extraction pipeline with context injection."""
    from genesis.mcp.memory.knowledge import _get_orchestrator

    orchestrator = _get_orchestrator()

    result = await orchestrator.ingest_source(
        file_path,
        project_type=project_type,
        domain=domain,
        purpose=purpose,
        user_context=context if context else None,
    )

    if result.error:
        raise RuntimeError(result.error)

    return result.unit_ids


def _move_to_completed(file_path: str, upload_id: str) -> str:
    """Move an ingested file to the completed directory. Returns new path."""
    src = Path(file_path)
    if not src.exists():
        return file_path

    dest_dir = _COMPLETED_DIR / upload_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.move(str(src), str(dest))

    # Clean up empty inbox directory
    inbox_dir = src.parent
    if inbox_dir.exists() and not any(inbox_dir.iterdir()):
        inbox_dir.rmdir()

    return str(dest)


async def recover_stale_processing() -> int:
    """Mark uploads stuck in 'processing' as failed.

    Called on server startup to recover from crashes during extraction.
    Returns the number of recovered uploads.
    """
    from datetime import UTC, datetime, timedelta

    from genesis.db.crud import knowledge_uploads
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if rt.db is None:
        return 0

    stale_cutoff = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    stale = await knowledge_uploads.list_processing_before(rt.db, stale_cutoff)

    count = 0
    for upload in stale:
        await knowledge_uploads.update_status(
            rt.db, upload["id"],
            status="failed",
            error_message="Interrupted by server restart",
        )
        logger.warning(
            "Recovered stale upload %s (%s) — marked as failed",
            upload["id"], upload.get("filename", "?"),
        )
        count += 1

    if count:
        logger.info("Recovered %d stale processing uploads", count)
    return count
