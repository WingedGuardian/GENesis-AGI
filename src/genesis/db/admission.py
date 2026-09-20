"""Database admission fencing — read side (PR-0 of the admission ladder).

The 2026-09-18/19 corruption incident on a live install showed script-side raw
openers (audit hook, edit sensor, and friends) writing to a database that the
integrity layer had already QUARANTINED: the central connectors refuse, but a
raw ``sqlite3.connect`` in ``scripts/`` consults nothing. This module is the
single read-side predicate those openers consult before touching the database.

Two independent fence sources, either one refuses:

- **Quarantine** (``genesis.db.integrity``): "this artifact failed integrity" —
  inode-bound by design, so a verified replacement naturally clears it.
- **Maintenance fence** (this module's marker): "an operator/maintenance owner
  holds this PATH for replacement" — path-bound ON PURPOSE, so it survives an
  atomic swap until the owner verifies and releases. The write side (leases,
  ``begin_maintenance``/``verify_and_release``) lands with the full admission
  module; this read side defines the marker location and the fail direction so
  the fence binds every script opener from PR-0 onward.

Fail direction: **closed**. Every consumer of :func:`database_is_fenced` is a
best-effort auxiliary reader/writer (audit trails, context injection, advisory
hooks) whose designed degrade is "skip the database this once" — so an
unreadable marker, a permission error, or any unexpected failure reads as
FENCED, never as clear. A skipped advisory write is recoverable; a write to a
fenced database is the incident class this exists to stop.

Dependency rule: stdlib + ``genesis.env``/``genesis.db.integrity`` only (both
stdlib-clean), so CC hook processes can import this cheaply. Never import
``connection.py``, aiosqlite, or anything async here — the dependency direction
is ``connection -> admission``, one way.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from genesis.db.integrity import database_is_quarantined
from genesis.env import genesis_home

__all__ = [
    "admission_dir",
    "database_is_fenced",
    "maintenance_fence_active",
    "maintenance_marker_path",
]


def admission_dir() -> Path:
    """Directory holding admission markers and (from PR-1) lock files.

    Lives beside ``db_quarantine.json`` under the genesis home — the same
    file-plane and conventions as the quarantine marker, namespaced into one
    directory because admission carries several files per database domain.
    Deliberately NOT backed up: the fence is live operational state of THIS
    install; restoring a stale fence onto a rebuilt install would wrongly
    fence it, and restore itself runs inside the fence.
    """
    return genesis_home() / "db_admission"


def _coerce_path(db_path) -> Path:
    """Accept a filesystem path OR a SQLite ``file:`` URI; return the path.

    Some script openers connect via URI (``file:...?mode=ro``). Keying the
    fence off the raw URI string would resolve to a nonexistent path, silently
    un-fencing exactly those callers — so the URI is parsed to its underlying
    path (query dropped, percent-encoding decoded) before any check.
    """
    text = str(db_path)
    if text.startswith("file:"):
        parts = urlsplit(text)
        text = url2pathname(parts.path) if parts.path else parts.netloc
    return Path(text).expanduser()


def _domain_key(db_path: Path) -> str:
    """Stable per-database key: readable stem + hash of the RESOLVED path.

    Resolution converges symlinked and relative aliases onto one admission
    domain. A hard link at a different path does NOT converge — the lease
    layer (PR-1) refuses that via a dev/ino cross-check rather than silently
    running a second, independent domain. Key from the argument, never from a
    cached module constant: tests and multi-worktree runs pass explicit paths.
    """
    resolved = str(_coerce_path(db_path).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return f"genesis-db-{digest}"


def maintenance_marker_path(db_path: Path) -> Path:
    """Where the path-scoped maintenance fence marker lives for *db_path*."""
    return admission_dir() / f"{_domain_key(db_path)}.maintenance.json"


def maintenance_fence_active(db_path: Path) -> bool:
    """True when a maintenance fence marker exists (or cannot be ruled out).

    Existence IS the fence — content is owner metadata for diagnostics, so an
    unreadable or malformed marker still fences (fail closed, mirroring the
    quarantine reader's malformed-marker direction). A missing admission
    directory is the ordinary empty state on a fresh install: no fence.
    """
    try:
        marker = maintenance_marker_path(db_path)
    except OSError:
        return True  # cannot even resolve state -> cannot rule the fence out
    try:
        return marker.exists() or marker.is_symlink()
    except OSError:
        return True


def database_is_fenced(db_path: Path) -> bool:
    """Single predicate for script-side openers: refuse when True.

    Quarantine OR maintenance fence — and fail closed on any error in either
    check, because every caller's degrade is an advisory skip.
    """
    try:
        path = _coerce_path(db_path)
    except Exception:
        return True
    try:
        if database_is_quarantined(path):
            return True
    except Exception:
        return True
    return maintenance_fence_active(path)
