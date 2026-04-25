"""Knowledge upload routes — file upload, ingest dispatch, status polling, taxonomy."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import uuid
from pathlib import Path

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)

_UPLOAD_DIR = Path.home() / ".genesis" / "knowledge" / "inbox"
_COMPLETED_DIR = Path.home() / ".genesis" / "knowledge" / "completed"

# Sanitize filenames: allow alphanumeric, dots, hyphens, underscores, spaces.
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._\- ]")
_MAX_FILENAME_LEN = 255


def _sanitize_filename(name: str) -> str:
    """Sanitize an uploaded filename to prevent path traversal."""
    # Strip directory components
    name = Path(name).name
    # Remove unsafe characters
    name = _SAFE_FILENAME_RE.sub("_", name)
    # Strip leading/trailing dots and spaces to prevent . / .. traversal
    name = name.strip(". ")
    # Truncate
    if len(name) > _MAX_FILENAME_LEN:
        stem = Path(name).stem[:_MAX_FILENAME_LEN - len(Path(name).suffix) - 1]
        name = stem + Path(name).suffix
    return name or "unnamed"


@blueprint.route("/api/genesis/knowledge/upload", methods=["POST"])
@_async_route
async def knowledge_upload():
    """Accept a multipart file upload, save to inbox, create tracking record.

    Returns upload_id and file metadata for the confirmation dialog.
    """
    from genesis.db.crud import knowledge_uploads
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    # Sanitize and prepare storage
    safe_name = _sanitize_filename(file.filename)
    upload_id = str(uuid.uuid4())
    upload_dir = _UPLOAD_DIR / upload_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_path = upload_dir / safe_name

    # Save file
    file.save(str(file_path))
    file_size = file_path.stat().st_size
    mime_type = mimetypes.guess_type(safe_name)[0]

    # Create DB record
    await knowledge_uploads.insert(
        rt.db,
        id=upload_id,
        filename=safe_name,
        file_path=str(file_path),
        file_size=file_size,
        mime_type=mime_type,
    )

    logger.info("Knowledge upload received: %s (%s, %d bytes)", safe_name, mime_type, file_size)

    return jsonify({
        "upload_id": upload_id,
        "filename": safe_name,
        "file_size": file_size,
        "mime_type": mime_type,
    })


@blueprint.route("/api/genesis/knowledge/ingest", methods=["POST"])
@_async_route
async def knowledge_ingest_upload():
    """Start ingestion of a previously uploaded file.

    Expects JSON body: {upload_id, project_type, domain, purpose?}
    Dispatches async ingestion to the main event loop and returns immediately.
    """
    from genesis.db.crud import knowledge_uploads
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    data = request.get_json(silent=True) or {}
    upload_id = data.get("upload_id")
    project_type = data.get("project_type")
    domain = data.get("domain", "auto")
    purpose = data.get("purpose")
    context = data.get("context", "")  # User-provided document context
    mode = data.get("mode", "extract")  # "extract" or "store"

    if not upload_id or not project_type:
        return jsonify({"error": "upload_id and project_type required"}), 400

    if mode not in ("extract", "store"):
        return jsonify({"error": "mode must be 'extract' or 'store'"}), 400

    # Atomic status transition: uploaded -> processing (prevents double-ingestion)
    purpose_list = [p.strip() for p in purpose.split(",")] if purpose else None
    transitioned = await knowledge_uploads.atomic_transition(
        rt.db, upload_id,
        from_status="uploaded",
        to_status="processing",
        project_type=project_type,
        domain=domain,
        purpose=purpose,
    )
    if not transitioned:
        # Either doesn't exist or already processing/completed
        upload = await knowledge_uploads.get(rt.db, upload_id)
        if upload is None:
            return jsonify({"error": "Upload not found"}), 404
        return jsonify({"error": f"Upload in '{upload['status']}' state, expected 'uploaded'"}), 409

    # Fire-and-forget: _async_route already runs us on the main event loop,
    # so create_task schedules directly without an extra cross-thread hop.
    from genesis.knowledge.ingest_upload import run_ingest

    asyncio.create_task(run_ingest(
        upload_id,
        project_type=project_type,
        domain=domain,
        purpose=purpose_list,
        context=context,
        mode=mode,
    ))

    return jsonify({
        "upload_id": upload_id,
        "status": "processing",
    })


@blueprint.route("/api/genesis/knowledge/upload/<upload_id>/status")
@_async_route
async def knowledge_upload_status(upload_id: str):
    """Poll upload/ingestion status."""
    from genesis.db.crud import knowledge_uploads
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    upload = await knowledge_uploads.get(rt.db, upload_id)
    if upload is None:
        return jsonify({"error": "Upload not found"}), 404

    return jsonify(upload)


@blueprint.route("/api/genesis/knowledge/upload/<upload_id>", methods=["DELETE"])
@_async_route
async def knowledge_upload_cancel(upload_id: str):
    """Cancel/delete an upload — removes file from disk and DB record."""
    from genesis.db.crud import knowledge_uploads
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    upload = await knowledge_uploads.get(rt.db, upload_id)
    if upload is None:
        return jsonify({"error": "Upload not found"}), 404

    if upload["status"] == "processing":
        return jsonify({"error": "Cannot cancel — ingestion in progress"}), 409

    # Remove file from disk
    file_path = Path(upload["file_path"])
    if file_path.exists():
        file_path.unlink()
    # Clean up empty parent directory
    if file_path.parent.exists() and not any(file_path.parent.iterdir()):
        file_path.parent.rmdir()

    await knowledge_uploads.delete(rt.db, upload_id)
    return jsonify({"status": "ok"})


@blueprint.route("/api/genesis/knowledge/uploads")
@_async_route
async def knowledge_uploads_list():
    """List recent uploads with status for the dashboard panel."""
    from genesis.db.crud import knowledge_uploads
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    limit = max(1, min(request.args.get("limit", 20, type=int), 50))
    uploads = await knowledge_uploads.list_recent(rt.db, limit=limit)
    return jsonify({"uploads": uploads})


@blueprint.route("/api/genesis/knowledge/taxonomy")
@_async_route
async def knowledge_taxonomy():
    """Return distinct project_type and domain values for autocomplete."""
    from genesis.db.crud import knowledge_uploads
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    tax = await knowledge_uploads.taxonomy(rt.db)
    return jsonify(tax)
