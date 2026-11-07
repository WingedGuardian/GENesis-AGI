"""Knowledge upload routes — file upload, ingest dispatch, status polling, taxonomy."""

from __future__ import annotations

import errno
import logging
import mimetypes
import os
import re
import stat
import uuid
from pathlib import Path

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint
from genesis.memory.reference_ops import REFERENCE_PROJECT

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


def _own_entry(dir_fd: int, wanted: str) -> str | None:
    """Return the directory's own name for the entry called ``wanted``, or None.

    Callers address the entry with the returned name, so what reaches the
    filesystem is something the directory listed, never request or row data.
    """
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            if entry.name == wanted:
                return entry.name
    return None


def _remove_upload_file(upload_id: str, stored_path: str) -> str | None:
    """Delete an upload's file and its emptied directory through DESCRIPTORS.

    Returns None when the goal state holds (the file is gone), or the reason
    nothing was touched. The upload directory is opened relative to the upload
    root with ``O_NOFOLLOW``, and the file is addressed by bare name relative to
    that descriptor, so no symlink below the root is ever followed and the
    stored path is never used to address anything. Same pattern as
    ``_purge_dir_entries`` in ``genesis.cc.invoker``.

    The one comparison on the stored path is a consistency check on the row, not
    the containment boundary: a row that does not describe
    ``<root>/<upload_id>/<name>`` is left for a human instead of being dropped.
    """
    if os.path.dirname(stored_path) != os.path.join(str(_UPLOAD_DIR), upload_id):
        return "Stored file path is not this upload's file"
    # The name must be one the upload route could have written: it stores
    # `_sanitize_filename(...)`. Anything else (`.`, `..`, an empty name from a
    # trailing slash, NUL, newline, an overlong name) never matches a listed
    # entry, so it would read as "already removed" and drop the row while the
    # real file stays on disk.
    name = os.path.basename(stored_path)
    if not name or name != _sanitize_filename(name):
        return "Stored file path is not this upload's file"

    try:
        root_fd = os.open(_UPLOAD_DIR, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("Could not open the upload root %s", _UPLOAD_DIR, exc_info=True)
        return "Upload directory could not be opened"
    try:
        try:
            dir_name = _own_entry(root_fd, upload_id)
        except OSError:
            logger.warning("Could not list the upload root %s", _UPLOAD_DIR, exc_info=True)
            return "Upload directory could not be opened"
        if dir_name is None:
            return None
        try:
            dir_fd = os.open(
                dir_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
            )
        except OSError:
            # A symlink (ELOOP, including one that points at itself) or a
            # non-directory where the upload's directory should be.
            return "Upload directory is not a real directory"
        try:
            file_name = _own_entry(dir_fd, name)
            if file_name is None:
                # Not listed means already removed only if nothing else is in
                # the upload's directory. If something is, the row names a file
                # that is not there (a case or spelling mismatch) and the real
                # file would be orphaned: leave both for a human.
                with os.scandir(dir_fd) as entries:
                    if any(True for _ in entries):
                        return "Stored file is not in the upload directory, which is not empty"
            else:
                if not stat.S_ISREG(os.lstat(file_name, dir_fd=dir_fd).st_mode):
                    return "Stored file path is not a regular file"
                # Unlinks the entry itself. If it is swapped for a link after
                # the lstat, the link is removed and its target is untouched.
                os.unlink(file_name, dir_fd=dir_fd)
        except OSError:
            logger.warning("Could not remove upload file %s", stored_path, exc_info=True)
            return "Stored file could not be removed"
        finally:
            os.close(dir_fd)
        try:
            os.rmdir(dir_name, dir_fd=root_fd)
        except OSError as exc:
            # Other files still in the directory is expected; anything else is worth a line.
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                logger.warning("Could not remove upload directory for %s", upload_id, exc_info=True)
    finally:
        os.close(root_fd)
    return None


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

    # Reference rows carry a structured body that ``parse_reference_body``
    # requires, and the paths that produce it are the reference-store MCP tools
    # and the auto-capture extraction pipeline — NOT this route.
    #
    # Deliberately NOT stated as "the partition has exactly one writer": an
    # earlier revision of this comment said that and named a module which
    # contains no writer at all. What this guard closes is the HTTP upload door
    # specifically. The free-form ``project`` / ``project_type`` arguments on
    # the ingest tools can still land an arbitrary body in the partition, so the
    # invariant belongs at the shared ingest chokepoint rather than here; that
    # is tracked separately. Claiming it here would be a structural guarantee
    # this route cannot make.
    # A document ingested here would satisfy neither: the References browser
    # renders it blank (that parser fails CLOSED and never falls back to the
    # raw body) and the knowledge browser excludes the partition outright — so
    # the document would be reachable through neither, which is data loss
    # wearing the shape of a successful upload.
    if project_type == REFERENCE_PROJECT:
        return jsonify({
            "error": f"project_type '{REFERENCE_PROJECT}' is reserved for the reference store",
            "detail": "Reference entries are created through the references API, "
                      "not by document upload. Choose a different project type.",
        }), 400

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

    # Fire-and-forget: _async_route already runs us on the main event loop, so
    # create_task schedules directly without an extra cross-thread hop. Use
    # tracked_task so any failure that escapes run_ingest's own handling (e.g.
    # an unexpected error before its inner try) is logged with context instead
    # of being silently swallowed by a bare create_task.
    from genesis.knowledge.ingest_upload import run_ingest
    from genesis.util.tasks import tracked_task

    tracked_task(
        run_ingest(
            upload_id,
            project_type=project_type,
            domain=domain,
            purpose=purpose_list,
            context=context,
            mode=mode,
        ),
        name=f"ingest-{upload_id}",
        logger=logger,
    )

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

    # Upload ids are minted by uuid4() in the upload route. Anything else never
    # names a directory this route created, so it is not used to address one.
    try:
        is_own_id = str(uuid.UUID(upload_id)) == upload_id
    except ValueError:
        is_own_id = False
    refusal = (
        _remove_upload_file(upload_id, upload["file_path"])
        if is_own_id
        else "Upload id is not a UUID"
    )
    if refusal:
        logger.error(
            "Refusing to cancel upload %s (stored path %s): %s",
            upload_id, upload["file_path"], refusal,
        )
        return jsonify({"error": refusal}), 409

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
