"""General-purpose file browser routes.

Provides directory listing, file read/write/create/rename/delete for the
dashboard.  Restricted to an allowlist of root directories for security.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from pathlib import Path

from flask import jsonify, request, send_file

from genesis.dashboard._blueprint import blueprint

logger = logging.getLogger(__name__)

# ── Allowed roots ─────────────────────────────────────────────────────

_HOME = Path.home()
_ALLOWED_ROOTS: list[Path] = [
    _HOME / "genesis",
    _HOME / ".genesis",
    _HOME / ".claude",
]

# Files that must never be read or written via the browser
_BLOCKED_NAMES = frozenset({
    "secrets.env",
    ".env",
    "credentials.json",
    "service-account.json",
})

_MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB read/write limit
_MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB upload limit
_UPLOAD_DIR = _HOME / ".genesis" / "uploads"

# Sanitize filenames: allow alphanumeric, dots, hyphens, underscores, spaces.
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._\- ]")
_MAX_FILENAME_LEN = 255


def _sanitize_filename(name: str) -> str:
    """Sanitize an uploaded filename to prevent path traversal."""
    name = Path(name).name
    name = _SAFE_FILENAME_RE.sub("_", name)
    name = name.strip(". ")
    if len(name) > _MAX_FILENAME_LEN:
        stem = Path(name).stem[: _MAX_FILENAME_LEN - len(Path(name).suffix) - 1]
        name = stem + Path(name).suffix
    return name or "unnamed"


def _deduplicate_filename(directory: Path, name: str) -> str:
    """If *name* already exists in *directory*, append -1, -2, etc."""
    dest = directory / name
    if not dest.exists():
        return name
    stem = Path(name).stem
    suffix = Path(name).suffix
    counter = 1
    while (directory / f"{stem}-{counter}{suffix}").exists():
        counter += 1
    return f"{stem}-{counter}{suffix}"


def _sanitize_relpath(relpath: str | None) -> list[str]:
    """Split a client-supplied relative path into traversal-safe segments.

    Used by folder uploads to preserve directory structure under the uploads
    root. Each segment is reduced to its basename, run through the filename
    charset filter, and stripped of leading/trailing dots/spaces — so empty,
    ``.``, and ``..`` segments collapse away and can never escape the base
    directory. Returns ``[]`` for empty/invalid input.
    """
    segments: list[str] = []
    for raw in re.split(r"[\\/]+", relpath or ""):
        seg = _SAFE_FILENAME_RE.sub("_", Path(raw).name).strip(". ")
        if seg and len(seg) <= _MAX_FILENAME_LEN:
            segments.append(seg)
    return segments


def _is_allowed(path: Path) -> bool:
    """Check that *path* resolves inside an allowed root and isn't blocked."""
    resolved = path.resolve()
    if resolved.name.lower() in _BLOCKED_NAMES:
        return False
    # Block paths containing "secret" in any component (except dir names "secrets"/".secrets")
    for part in resolved.parts:
        if "secret" in part.lower() and part.lower() not in ("secrets", ".secrets"):
            return False
    return any(resolved.is_relative_to(root.resolve()) for root in _ALLOWED_ROOTS)


def _sanitize_path(raw: str | None) -> tuple[Path | None, tuple | None]:
    """Resolve and validate a user-supplied path.

    Returns ``(resolved_path, None)`` on success, or
    ``(None, (error_dict, status_code))`` on failure. Centralizes path
    canonicalization + allowlist validation so user input never reaches
    filesystem operations without sanitization.
    """
    if not raw:
        return None, ({"error": "path required"}, 400)
    resolved = Path(raw).resolve()
    if not _is_allowed(resolved):
        return None, ({"error": "Path not allowed"}, 403)
    return resolved, None


def _file_info(p: Path) -> dict:
    """Return metadata dict for a single path."""
    try:
        st = p.stat()
    except OSError:
        return {"name": p.name, "error": "stat failed"}

    return {
        "name": p.name,
        "path": str(p),
        "is_dir": p.is_dir(),
        "size": st.st_size if not p.is_dir() else None,
        "modified": st.st_mtime,
        "permissions": stat.filemode(st.st_mode),
    }


# ── Routes ────────────────────────────────────────────────────────────

@blueprint.route("/api/genesis/files")
def file_list():
    """List directory contents.

    Query params:
        path – absolute directory path (default: ~/genesis)
    """
    raw_path = request.args.get("path", str(_HOME / "genesis"))
    target = Path(raw_path).resolve()

    # Allow listing the home directory for navigation between roots,
    # but do NOT add _HOME to _ALLOWED_ROOTS (that would expose ~/.ssh etc.
    # to read/write/delete endpoints). Only file_list gets this exception.
    if target != _HOME.resolve() and not _is_allowed(target):
        return jsonify({"error": "Path not allowed"}), 403

    if not target.is_dir():
        return jsonify({"error": "Not a directory"}), 400

    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403

    items = []
    for entry in entries:
        # Skip hidden files except known safe directories
        if entry.name.startswith(".") and entry.name not in (".claude", ".genesis"):
            continue
        if entry.name.lower() in _BLOCKED_NAMES:
            continue
        items.append(_file_info(entry))

    return jsonify({
        "path": str(target),
        "parent": str(target.parent) if target != target.parent and target.resolve() != _HOME.resolve() else None,
        "entries": items,
    })


@blueprint.route("/api/genesis/files/read")
def file_read():
    """Read a file's content.

    Query params:
        path – absolute file path
    """
    target, err = _sanitize_path(request.args.get("path"))
    if err:
        return jsonify(err[0]), err[1]
    if not target.is_file():
        return jsonify({"error": "Not a file"}), 404
    if target.stat().st_size > _MAX_FILE_SIZE:
        return jsonify({"error": f"File too large (>{_MAX_FILE_SIZE // 1024}KB)"}), 413

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    # Guess syntax mode for Ace editor
    suffix = target.suffix.lower()
    mode_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".json": "json", ".yaml": "yaml", ".yml": "yaml",
        ".md": "markdown", ".html": "html", ".css": "css",
        ".sh": "sh", ".bash": "sh", ".toml": "toml",
        ".sql": "sql", ".xml": "xml",
    }

    return jsonify({
        "path": str(target),
        "name": target.name,
        "content": content,
        "size": len(content),
        "mode": mode_map.get(suffix, "text"),
        "writable": os.access(target, os.W_OK),
    })


@blueprint.route("/api/genesis/files/write", methods=["PUT"])
def file_write():
    """Write content to a file.

    JSON body: {path: str, content: str}
    """
    data = request.get_json(silent=True) or {}
    content = data.get("content")

    target, err = _sanitize_path(data.get("path"))
    if err:
        return jsonify(err[0]), err[1]
    if content is None:
        return jsonify({"error": "content required"}), 400
    if not target.exists():
        return jsonify({"error": "File not found — use create endpoint"}), 404
    if len(content.encode("utf-8")) > _MAX_FILE_SIZE:
        return jsonify({"error": "Content too large"}), 413

    try:
        target.write_text(content, encoding="utf-8")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"status": "ok", "path": str(target), "size": len(content)})


@blueprint.route("/api/genesis/files/create", methods=["POST"])
def file_create():
    """Create a new file or directory.

    JSON body: {path: str, is_dir: bool (default false), content: str (optional)}
    """
    data = request.get_json(silent=True) or {}
    is_dir = data.get("is_dir", False)

    target, err = _sanitize_path(data.get("path"))
    if err:
        return jsonify(err[0]), err[1]
    if target.exists():
        return jsonify({"error": "Already exists"}), 409

    # Parent must exist and be allowed
    if not target.parent.is_dir():
        return jsonify({"error": "Parent directory does not exist"}), 400

    try:
        if is_dir:
            target.mkdir(parents=False)
        else:
            content = data.get("content", "")
            target.write_text(content, encoding="utf-8")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"status": "ok", "path": str(target)}), 201


@blueprint.route("/api/genesis/files/rename", methods=["POST"])
def file_rename():
    """Rename or move a file/directory.

    JSON body: {path: str, new_name: str}
    """
    data = request.get_json(silent=True) or {}
    new_name = data.get("new_name")

    source, err = _sanitize_path(data.get("path"))
    if err:
        return jsonify(err[0]), err[1]
    if not new_name:
        return jsonify({"error": "new_name required"}), 400

    if "/" in new_name or "\\" in new_name:
        return jsonify({"error": "new_name must be a filename, not a path"}), 400
    if not source.exists():
        return jsonify({"error": "Source not found"}), 404

    dest = source.parent / new_name
    if not _is_allowed(dest):
        return jsonify({"error": "Destination path not allowed"}), 403
    if dest.exists():
        return jsonify({"error": "Destination already exists"}), 409

    try:
        source.rename(dest)
    except Exception:
        logger.exception("Failed to rename %s", source)
        return jsonify({"error": "Rename failed"}), 500

    return jsonify({"status": "ok", "old_path": str(source), "new_path": str(dest)})


@blueprint.route("/api/genesis/files/delete", methods=["DELETE"])
def file_delete():
    """Delete a file (not directories, for safety).

    Query params: path – absolute file path
    """
    target, err = _sanitize_path(request.args.get("path"))
    if err:
        return jsonify(err[0]), err[1]
    if not target.exists():
        return jsonify({"error": "Not found"}), 404
    if target.is_dir():
        return jsonify({"error": "Cannot delete directories via browser — use terminal"}), 400

    try:
        target.unlink()
    except Exception:
        logger.exception("Failed to delete %s", target)
        return jsonify({"error": "Delete failed"}), 500

    return jsonify({"status": "ok", "path": str(target)})


@blueprint.route("/api/genesis/files/download")
def file_download():
    """Download a file as an attachment.

    Query params:
        path – absolute file path
    """
    target, err = _sanitize_path(request.args.get("path"))
    if err:
        return jsonify(err[0]), err[1]
    if not target.is_file():
        return jsonify({"error": "Not a file"}), 404
    if target.stat().st_size > _MAX_UPLOAD_SIZE:
        return jsonify({"error": f"File too large (>{_MAX_UPLOAD_SIZE // (1024 * 1024)}MB)"}), 413

    try:
        return send_file(
            target,
            as_attachment=True,
            download_name=target.name,
            mimetype="application/octet-stream",
        )
    except FileNotFoundError:
        return jsonify({"error": "File no longer exists"}), 404


@blueprint.route("/api/genesis/files/upload", methods=["POST"])
def file_upload():
    """Upload a file to ~/.genesis/uploads/ via multipart form.

    No processing or knowledge base involvement — just filesystem storage.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    # Pre-check content length before saving to disk
    if request.content_length and request.content_length > _MAX_UPLOAD_SIZE:
        return jsonify({"error": f"File too large (>{_MAX_UPLOAD_SIZE // (1024 * 1024)}MB)"}), 413

    # Optional folder upload: ``relpath`` carries the file's path within a
    # dropped directory (e.g. "Project/data/notes.txt"). All segments are
    # sanitized to be traversal-safe; the leading ones become subdirectories
    # under the uploads root, the last is the filename. Absent/flat uploads
    # fall back to the file's own name (unchanged single-file behavior).
    rel_segments = _sanitize_relpath(request.form.get("relpath"))
    if rel_segments:
        *subdirs, base_name = rel_segments
    else:
        subdirs, base_name = [], _sanitize_filename(file.filename)
    base_name = base_name or _sanitize_filename(file.filename)

    dest_dir = _UPLOAD_DIR.joinpath(*subdirs)

    # Containment check BEFORE any filesystem write. The explicit
    # is_relative_to(_UPLOAD_DIR) guard ensures a crafted relpath can never
    # escape the uploads root (resolve() works on not-yet-created paths).
    if not dest_dir.resolve().is_relative_to(_UPLOAD_DIR.resolve()):
        return jsonify({"error": "Path not allowed"}), 403

    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _deduplicate_filename(dest_dir, base_name)
    dest = dest_dir / safe_name

    # Leaf-level guard: blocked names (secrets.env, .env, …) and allowlist.
    if not _is_allowed(dest):
        return jsonify({"error": "Path not allowed"}), 403

    # Save and verify size (content_length can be spoofed, so double-check)
    file.save(str(dest))
    file_size = dest.stat().st_size

    if file_size > _MAX_UPLOAD_SIZE:
        dest.unlink()
        return jsonify({"error": f"File too large (>{_MAX_UPLOAD_SIZE // (1024 * 1024)}MB)"}), 413

    rel_display = "/".join([*subdirs, safe_name])
    logger.info("File uploaded: %s (%d bytes)", rel_display, file_size)

    return jsonify({
        "path": str(dest),
        "filename": rel_display,
        "size": file_size,
    })
