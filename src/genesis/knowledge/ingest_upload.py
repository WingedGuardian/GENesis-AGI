"""Ingestion worker for dashboard uploads.

Thin bridge: updates DB status → calls KnowledgeOrchestrator.ingest_source →
updates DB status with results. Called via run_coroutine_threadsafe from the
upload route handler.
"""

from __future__ import annotations

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
) -> None:
    """Run the full ingestion pipeline for an uploaded file.

    Updates the knowledge_uploads row with results on completion or failure.
    Moves the file to the completed directory on success.
    """
    from genesis.db.crud import knowledge_uploads
    from genesis.mcp.memory.knowledge import _get_orchestrator
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
        orchestrator = _get_orchestrator()
        result = await orchestrator.ingest_source(
            file_path,
            project_type=project_type,
            domain=domain,
            purpose=purpose,
        )

        if result.error:
            await knowledge_uploads.update_status(
                rt.db, upload_id,
                status="failed",
                error_message=result.error,
            )
            logger.warning("Ingest failed for %s: %s", filename, result.error)
            return

        # Move file to completed directory
        _move_to_completed(file_path, upload_id)

        await knowledge_uploads.update_status(
            rt.db, upload_id,
            status="completed",
            unit_ids=result.unit_ids,
        )

        logger.info(
            "Ingest completed for %s: %d units created",
            filename, result.units_created,
        )

    except Exception:
        logger.exception("Ingest worker failed for upload %s", upload_id)
        try:
            await knowledge_uploads.update_status(
                rt.db, upload_id,
                status="failed",
                error_message="Internal error during ingestion",
            )
        except Exception:
            logger.exception("Failed to update upload status after error")


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
