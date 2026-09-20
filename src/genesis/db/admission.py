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

from genesis.db.integrity import (
    DatabaseIntegrityError,
    assert_not_quarantined,
    database_is_quarantined,
)
from genesis.env import genesis_home

__all__ = [
    "DatabaseFencedError",
    "admission_dir",
    "assert_admitted",
    "database_is_fenced",
    "maintenance_fence_active",
    "maintenance_marker_path",
    "maintenance_marker_paths",
]


class DatabaseFencedError(DatabaseIntegrityError):
    """Raised by :func:`assert_admitted` when a maintenance fence is active.

    Subclasses :class:`~genesis.db.integrity.DatabaseIntegrityError` (itself a
    ``sqlite3.DatabaseError``) ON PURPOSE: every existing handler of either
    already catches this, so adding the maintenance fence to the connection
    factories cannot turn a previously-handled refusal into an escaping
    exception. The distinct type exists so a caller that wants to tell
    "corrupt" from "an operator is holding this path" can, without any caller
    being required to.
    """


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


def _domain_keys(db_path: Path) -> list[str]:
    """Every key a marker for *db_path* may legitimately live under.

    TWO keys, not one, and the reason is the exact moment this fence exists
    for. Hashing only the RESOLVED path converges symlinked and relative
    aliases onto one domain — good — but when the configured path IS a
    symlink, an atomic replacement changes what it resolves to, so the marker
    silently changes identity DURING the replacement it was written to
    survive, and the fence lifts with no owner releasing it (Codex P2,
    reproduced: a marker written for ``db -> target1`` is invisible once the
    link points at ``target2``). Hashing only the LEXICAL path has the
    opposite hole: two aliases for one database become two domains, so a
    caller arriving by the other spelling walks straight past the fence.

    Checking both closes both. The keys coincide for an ordinary regular-file
    database (the common case, and what both installs run today), so this
    costs a second ``stat`` only where a symlink is actually in play.

    Key from the argument, never from a cached module constant: tests and
    multi-worktree runs pass explicit paths.
    """
    coerced = _coerce_path(db_path)
    # Lexical: absolute(), NOT resolve() — no symlink traversal, so it cannot
    # move when a link is retargeted.
    lexical = _safe_absolute(coerced)
    resolved = _safe_resolve(coerced)
    if lexical is None or resolved is None:
        # A spelling we could not compute is a key we cannot check, and a
        # marker may be sitting under exactly that key. "Could not establish
        # state" is the fence condition, so raise and let the fail-closed
        # caller fence — never silently check the half we happen to have.
        #
        # MEASURED trap this guards: a symlink-loop path still yields a
        # perfectly good absolute() while resolve() fails, so proceeding with
        # the one usable key answered "not fenced" for a path whose real
        # identity was unknowable — turning a loud RuntimeError into a silent
        # fail-OPEN, which is the worse of the two.
        raise OSError(f"cannot derive both admission keys for {db_path!r}")
    candidates = [lexical] if lexical == resolved else [lexical, resolved]
    return [f"genesis-db-{hashlib.sha256(c.encode('utf-8')).hexdigest()[:16]}" for c in candidates]


def _safe_absolute(path: Path) -> str | None:
    try:
        return str(path.absolute())
    except (OSError, RuntimeError, ValueError):
        return None


def _safe_resolve(path: Path) -> str | None:
    """``resolve()`` that cannot escape as an exception.

    ``RuntimeError`` is deliberate and NOT defensive padding: on CPython 3.12
    ``Path.resolve(strict=False)`` raises ``RuntimeError("Symlink loop from
    ...")`` rather than ``OSError`` for a symlink loop (cpython #109187; the
    behaviour changes in 3.13). MEASURED against this runtime: without this
    catch a looped path escapes :func:`database_is_fenced` as a RuntimeError
    instead of fencing — the one error legitimate callers do not expect from a
    predicate documented as fail-closed.
    """
    try:
        return str(path.resolve())
    except (OSError, RuntimeError, ValueError):
        return None


def maintenance_marker_path(db_path: Path) -> Path:
    """The marker path a WRITER uses for *db_path* (the lexical-key form).

    Readers must use :func:`maintenance_marker_paths` — a writer publishes one
    marker, but a reader has to look under every spelling that could hold one.
    """
    return admission_dir() / f"{_domain_keys(db_path)[0]}.maintenance.json"


def maintenance_marker_paths(db_path: Path) -> list[Path]:
    """Every marker path a fence for *db_path* could be published under."""
    directory = admission_dir()
    return [directory / f"{key}.maintenance.json" for key in _domain_keys(db_path)]


def _marker_exists(marker: Path) -> bool:
    """Existence by ``lstat``, fail-closed, WITHOUT ``Path.exists()``.

    ``Path.exists()`` answers a different question than this fence needs: it
    follows symlinks (so a marker that is a dangling link reads as absent) and
    on some errors answers False. ``lstat`` distinguishes the one genuinely
    empty answer — the name is not there — from every other outcome, which
    means "state could not be established" and must fence.

    Note the narrow ENOENT-class allowance: ``FileNotFoundError`` and
    ``NotADirectoryError`` are the ordinary no-fence shapes (fresh install,
    no admission dir). Anything else — permissions, I/O error, ELOOP — fences.
    """
    try:
        marker.lstat()
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True


def maintenance_fence_active(db_path: Path) -> bool:
    """True when a maintenance fence marker exists (or cannot be ruled out).

    Existence IS the fence — content is owner metadata for diagnostics, so an
    unreadable or malformed marker still fences (fail closed, mirroring the
    quarantine reader's malformed-marker direction). A missing admission
    directory is the ordinary empty state on a fresh install: no fence.
    """
    try:
        markers = maintenance_marker_paths(db_path)
    except (OSError, RuntimeError, ValueError):
        return True  # cannot even resolve state -> cannot rule the fence out
    return any(_marker_exists(marker) for marker in markers)


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
    try:
        return maintenance_fence_active(path)
    except Exception:
        # maintenance_fence_active is itself fail-closed, so reaching here
        # means something unforeseen. The contract this function publishes is
        # "never raises, fences on doubt" — keep it true at the boundary too,
        # rather than relying on every caller to have its own guard.
        return True


def assert_admitted(path: str | Path) -> None:
    """Refuse a quarantined OR maintenance-fenced database. The factory seam.

    This is the assertion the connection factories in
    :mod:`genesis.db.connection` call at OPEN time, replacing bare
    :func:`~genesis.db.integrity.assert_not_quarantined`. Quarantine is
    delegated unchanged — same predicate, same exception, same message — so
    this strictly ADDS the maintenance fence rather than reinterpreting any
    existing refusal.

    Unlike :func:`database_is_fenced` (a predicate for best-effort script
    openers, whose degrade is an advisory skip) this RAISES, because its
    callers are the real connection paths where silently continuing would mean
    opening the database the fence exists to protect.
    """
    assert_not_quarantined(path)
    if maintenance_fence_active(Path(path) if not isinstance(path, Path) else path):
        raise DatabaseFencedError(
            f"database under an active maintenance fence; a maintenance owner holds "
            f"{path} for replacement. Inspect "
            f"{admission_dir()} and wait for release before reopening."
        )
