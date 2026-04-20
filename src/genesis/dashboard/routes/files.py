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

from flask import jsonify, request

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
    raw_path = request.args.get("path")
    if not raw_path:
        return jsonify({"error": "path required"}), 400

    target = Path(raw_path).resolve()
    if not _is_allowed(target):
        return jsonify({"error": "Path not allowed"}), 403
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
    raw_path = data.get("path")
    content = data.get("content")

    if not raw_path or content is None:
        return jsonify({"error": "path and content required"}), 400

    target = Path(raw_path).resolve()
    if not _is_allowed(target):
        return jsonify({"error": "Path not allowed"}), 403
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
    raw_path = data.get("path")
    is_dir = data.get("is_dir", False)

    if not raw_path:
        return jsonify({"error": "path required"}), 400

    target = Path(raw_path).resolve()
    if not _is_allowed(target):
        return jsonify({"error": "Path not allowed"}), 403
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
    raw_path = data.get("path")
    new_name = data.get("new_name")

    if not raw_path or not new_name:
        return jsonify({"error": "path and new_name required"}), 400

    if "/" in new_name or "\\" in new_name:
        return jsonify({"error": "new_name must be a filename, not a path"}), 400

    source = Path(raw_path).resolve()
    if not _is_allowed(source):
        return jsonify({"error": "Source path not allowed"}), 403
    if not source.exists():
        return jsonify({"error": "Source not found"}), 404

    dest = source.parent / new_name
    if not _is_allowed(dest):
        return jsonify({"error": "Destination path not allowed"}), 403
    if dest.exists():
        return jsonify({"error": "Destination already exists"}), 409

    try:
        source.rename(dest)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"status": "ok", "old_path": str(source), "new_path": str(dest)})


@blueprint.route("/api/genesis/files/delete", methods=["DELETE"])
def file_delete():
    """Delete a file (not directories, for safety).

    Query params: path – absolute file path
    """
    raw_path = request.args.get("path")
    if not raw_path:
        return jsonify({"error": "path required"}), 400

    target = Path(raw_path).resolve()
    if not _is_allowed(target):
        return jsonify({"error": "Path not allowed"}), 403
    if not target.exists():
        return jsonify({"error": "Not found"}), 404
    if target.is_dir():
        return jsonify({"error": "Cannot delete directories via browser — use terminal"}), 400

    try:
        target.unlink()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"status": "ok", "path": str(raw_path)})


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

    safe_name = _sanitize_filename(file.filename)

    # Ensure uploads directory exists
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Deduplicate filename
    safe_name = _deduplicate_filename(_UPLOAD_DIR, safe_name)
    dest = _UPLOAD_DIR / safe_name

    # Security check
    if not _is_allowed(dest):
        return jsonify({"error": "Path not allowed"}), 403

    # Save and check size
    file.save(str(dest))
    file_size = dest.stat().st_size

    if file_size > _MAX_UPLOAD_SIZE:
        dest.unlink()
        return jsonify({"error": f"File too large (>{_MAX_UPLOAD_SIZE // (1024 * 1024)}MB)"}), 413

    logger.info("File uploaded: %s (%d bytes)", safe_name, file_size)

    return jsonify({
        "path": str(dest),
        "filename": safe_name,
        "size": file_size,
    })
