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


#: SQLite databases and the sidecars a live connection keeps beside them.
#:
#: These must never be opened by a file route, and the reason is not privacy —
#: it is that OPENING ONE CORRUPTS IT. POSIX releases every record lock a process
#: holds on a file the moment that process closes ANY descriptor to it, not
#: merely the descriptor that took the lock. SQLite's unix VFS defers its own
#: closes to defend against this but cannot see a descriptor opened by other
#: code in the same process — and these routes run inside genesis-server, which
#: holds SQLite's locks on the live database.
#:
#: So a single read drops the server's lock. The next short-lived opener then
#: takes an exclusive lock, concludes it is the last connection, checkpoints its
#: partial view into the main file and unlinks the -wal/-shm, while the server
#: keeps writing through descriptors whose directory entries are gone. MEASURED:
#: an in-process open/close of a live ``-shm`` took its locks 2 -> 0, and the
#: same mechanism malformed a production database in under two minutes.
#:
#: NOT AN AUTH QUESTION. An authenticated read performs the identical open and
#: drops the identical locks — the owner browsing data/ in the dashboard's own
#: Files tab would do it. Authentication changes who can trigger it, never what
#: it does, so the block belongs on the FILE, not on the caller.
#:
#: NOTHING HERE OPENS OR STATS THE CALLER'S PATH, and that constraint is the
#: design rather than an implementation detail. It was learned the expensive way.
#: Earlier revisions added an identity check (comparing inodes against
#: ``/proc/self/fd``) and a content probe (reading the SQLite magic). The content
#: probe OPENED the live database inside the server — the exact operation this
#: guard exists to prevent — and was reachable whenever the identity check failed
#: open, which it does when ``/proc`` is unreadable and when the configured
#: database has no extension. Review REPRODUCED the consequence: a second process
#: acquired the previously-blocked write lock immediately after that probe
#: closed. A guard whose fallback performs the harm is worse than a guard with a
#: known gap, so both layers were removed rather than patched again.
#:
#: What remains cannot misfire, because it never touches the file:
#:   1. the CONFIGURED database path and everything beside it — read from config
#:      rather than guessed, so it covers the main database under any name
#:      including no extension at all, plus every sidecar;
#:   2. a name-suffix rule, for databases living elsewhere.
#:
#: STATED GAP, because pretending otherwise is what produced the removed code: a
#: database OUTSIDE the data directory under a name the suffix rule does not
#: recognise is NOT covered — the managed Chromium profile's ``Cookies``,
#: ``History`` and ``Web Data`` are the measured example. Closing that needs a
#: registry of the databases the server actually opens, which two independent
#: reviewers converged on and which is tracked separately. It is deliberately NOT
#: approximated here with a third heuristic.
#:
#: Accepted cost, stated: an archived database copy matching a suffix is blocked
#: even though opening one cannot affect the live file's locks. These are binary
#: files a text route would mangle anyway.

#: Bound on the rename guard's directory walk. A rename is a rare, interactive
#: operation, so the cost only ever lands on a human who asked for it; the bound
#: exists so a pathological tree cannot stall a request thread, not to save time
#: on the common case.
_RENAME_SCAN_MAX_ENTRIES = 20_000

_SQLITE_DB_SUFFIXES = (".db", ".db3", ".sqlite", ".sqlite3")
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-wal2", "-shm", "-journal")
#: SQLite's multi-database master journal: ``<db>-mj<8 hex><8 hex>``.
_MASTER_JOURNAL_RE = re.compile(r"-mj[0-9a-f]{4,}$", re.IGNORECASE)


def _is_sqlite_artifact(name: str) -> bool:
    """Is *name* a SQLite database, or a sidecar of one?

    Name-based and therefore incomplete by construction — see the note above.
    Covers databases outside the configured data directory; the authoritative
    check for the configured one is :func:`_in_configured_database_area`.
    """
    lowered = name.lower()
    lowered = _MASTER_JOURNAL_RE.sub("", lowered)
    for sidecar in _SQLITE_SIDECAR_SUFFIXES:
        if lowered.endswith(sidecar):
            lowered = lowered[: -len(sidecar)]
            break
    return lowered.endswith(_SQLITE_DB_SUFFIXES)


def _in_configured_database_area(resolved: Path) -> bool:
    """Is *resolved* the configured database, or anything beside it?

    The one AUTHORITATIVE check here: the path comes from config, not from a
    guess about names, so it covers the main database under any spelling —
    including the extensionless form ``genesis.env`` explicitly supports — and
    every sidecar it keeps in the same directory, present or future.

    Directory-scoped on purpose. A database keeps company: ``-wal``, ``-shm``,
    ``-journal``, master journals, pre-restore copies. Enumerating those by name
    is the approach that kept coming up short, and the data directory holds
    nothing a file browser needs to open anyway.

    Reads config but touches no filesystem: ``is_relative_to`` is pure path
    arithmetic. Fails OPEN if the path cannot be resolved, which is safe because
    the name rule still runs behind it — and unlike the removed probes, failing
    open here does nothing dangerous, it merely declines to add a reason.
    """
    try:
        from genesis.env import genesis_db_path

        db_path = genesis_db_path().resolve()
    except Exception:  # config unreadable — fall through to the name rule
        return False
    return resolved == db_path or resolved.is_relative_to(db_path.parent)


def _contains_live_database(directory: Path) -> bool:
    """Does *directory* hold a database, directly or in a subdirectory?

    Used by the rename guard: moving a directory relocates its contents without
    ever opening them, so the leaf-name checks in :func:`_is_allowed` cannot see
    it. Walks rather than globbing one level, because ``data/`` could be renamed
    by way of its parent.

    Bounded at ``_RENAME_SCAN_MAX_ENTRIES`` and fails CLOSED on the bound: a
    directory too large to scan is refused rather than waved through, since the
    only thing being refused is a rename the operator can perform another way.

    Uses ``os.scandir`` rather than ``Path.rglob``. That is not a style choice:
    ``rglob`` SILENTLY OMITS directories it cannot descend into, so this function
    could return False having never looked at part of the tree while its
    docstring claimed to fail closed. Review named the case — if the server holds
    ``tree/private/live.db`` and ``private`` loses search permission, renaming
    ``tree`` was permitted and relocated a live database. An unreadable subtree
    now refuses, which is what "fails closed" has to mean.

    Classifies children by NAME only, and deliberately does not open or stat
    them. An earlier revision consulted ``/proc/self/fd`` per child — O(entries x
    descriptors), measured at ~6s for 5,000 files against 300 descriptors on a
    request thread — and then read each one's header, which meant a rename
    request could itself drop the server's locks. Walking a directory is not a
    reason to touch every file in it.
    """
    seen = 0
    stack = [directory]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            # Cannot inspect it, so cannot clear it. Refuse.
            return True
        for entry in entries:
            seen += 1
            if seen > _RENAME_SCAN_MAX_ENTRIES:
                return True  # too big to clear — refuse rather than guess
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                return True  # same reasoning: unknown means refused
            if _is_sqlite_artifact(entry.name) or _in_configured_database_area(
                Path(entry.path)
            ):
                return True
    return False


def _is_allowed(path: Path) -> bool:
    """Check that *path* resolves inside an allowed root and isn't blocked.

    CONTAINMENT IS CHECKED FIRST, and that ordering is load-bearing rather than
    stylistic: every later check returns False on a match, so reordering cannot
    change the verdict — but it does change what this function TOUCHES. The
    identity check below stats the path, and the rename guard walks it. Doing
    either to a path that has not been proven to lie inside an allowed root is
    filesystem work on unvalidated input, which is what CodeQL's py/path-injection
    flagged here (3 high-severity alerts, all of them fair). Reject out-of-root
    paths before touching the filesystem at all.
    """
    resolved = path.resolve()
    if not any(resolved.is_relative_to(root.resolve()) for root in _ALLOWED_ROOTS):
        return False
    if resolved.name.lower() in _BLOCKED_NAMES:
        return False
    # Block paths containing "secret" in any component (except dir names "secrets"/".secrets")
    for part in resolved.parts:
        if "secret" in part.lower() and part.lower() not in ("secrets", ".secrets"):
            return False
    # PRIMARY: the configured database and everything beside it. Authoritative
    # rather than heuristic — the path comes from config, so it covers the main
    # database under any name (including none) and every sidecar. Pure path
    # arithmetic; touches nothing.
    if _in_configured_database_area(resolved):
        return False
    # SECONDARY: name, for databases living elsewhere. Checked on the RESOLVED
    # name, so a SYMLINK cannot smuggle one through under an innocent-looking
    # path. A HARDLINK still can — see the stated gap in the module note; that
    # needs the registry, not another guess.
    #
    # DIRECTORIES ARE EXEMPT from the name rule. A directory cannot be opened as
    # a database, so it cannot trigger the lock loss this guards, and blocking it
    # only made an existing directory named `project.db` or `fixtures.sqlite`
    # unlistable and unrenameable. A directory that CONTAINS a database is a
    # separate question, answered by the recursive guard in `file_rename`.
    #
    # Stated limit: a not-yet-existing path cannot be told apart from a file
    # here, so creating a NEW directory with a database-shaped name is still
    # refused. Narrow, and the caller can rename into it afterwards.
    try:
        if resolved.is_dir():
            return True
    except OSError:
        pass
    return not _is_sqlite_artifact(resolved.name)


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

    # `_is_allowed` inspects a LEAF name, so it cannot see that a directory
    # contains the live database. Renaming that directory does not open the
    # file — it is outside the "never open a database" rule — but the outcome is
    # the same loss: the server keeps writing through descriptors whose
    # directory entry has moved, and the next connection creates a fresh empty
    # database at the original path. MEASURED reachable (200) before this guard.
    if source.is_dir() and _contains_live_database(source):
        return jsonify({"error": "Directory contains a live database"}), 403

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
    # base_name is always non-empty here (_sanitize_relpath keeps only truthy
    # segments; _sanitize_filename defaults to "unnamed").

    dest_dir = _UPLOAD_DIR.joinpath(*subdirs)

    # Validate BEFORE any filesystem write. Two guards: containment to the
    # uploads root (defends even if sanitization ever regresses — resolve()
    # works on not-yet-created paths) and the leaf/component guard (blocked
    # names like secrets.env, "secret" in any path part, allowlist). Dedup
    # only appends a numeric suffix, so checking the pre-dedup name is correct
    # and avoids creating directories for a request we're about to reject.
    candidate = dest_dir / base_name
    if not dest_dir.resolve().is_relative_to(_UPLOAD_DIR.resolve()) or not _is_allowed(candidate):
        return jsonify({"error": "Path not allowed"}), 403

    # Create the validated destination and write. Guard the filesystem ops: a
    # relpath subdir can collide with an existing file (FileExistsError), and
    # writes can fail (disk full, permissions) — return a clean error, not a
    # 500 stack trace.
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _deduplicate_filename(dest_dir, base_name)
        dest = dest_dir / safe_name
        # Save and verify size (content_length can be spoofed, so double-check)
        file.save(str(dest))
        file_size = dest.stat().st_size
    except OSError as exc:
        logger.warning(
            "Upload failed for %r: %s", request.form.get("relpath") or file.filename, exc
        )
        return jsonify({"error": "Could not save file"}), 400

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
