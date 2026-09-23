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
from genesis.dashboard.auth import is_authenticated

logger = logging.getLogger(__name__)

# ── Allowed roots ─────────────────────────────────────────────────────

_HOME = Path.home()
_ALLOWED_ROOTS: list[Path] = [
    _HOME / "genesis",
    _HOME / ".genesis",
    _HOME / ".claude",
]

# Files that must never be read or written via the browser
_BLOCKED_NAMES = frozenset(
    {
        "secrets.env",
        ".env",
        "credentials.json",
        "service-account.json",
    }
)

# Vocabulary that marks a path component as credential-bearing. Matched
# STRUCTURALLY rather than by exact filename, because an exact-name list is a
# list of the spellings someone thought of: ``_BLOCKED_NAMES`` carried
# ``credentials.json`` while the real Claude Code file is ``.credentials.json``,
# and a single leading dot was enough to serve an OAuth token to an anonymous
# caller.
#
# Each rule below is anchored rather than substring-matched, and the anchoring
# is MEASURED, not chosen. Sweeping 87,219 non-vendor files under the three
# allowed roots: this vocabulary refuses 68 (0.078%), against 12 (0.014%) for
# the exact-name predicate it replaces. Of the 56 newly refused, 48 are genuine
# credentials on this install and 8 are prose or source files that merely end a
# word in "token" (``tokens.css``, ``session-tokens.ts``). An earlier draft
# matched a stem ANYWHERE in the component and scored 4.226% — 3,591 of those
# were one installed plugin whose NAME contains "token", i.e. a guard that
# makes a whole tree unbrowsable is one an operator routes around.
_CREDENTIAL_STEMS = frozenset(
    {
        "token",
        "tokens",
        "credential",
        "credentials",
        "cred",
        "creds",
        "passwd",
        "password",
        "passphrase",
    }
)

# Extensions whose whole purpose is to carry a key or an environment full of
# them. ``_BLOCKED_NAMES`` already listed bare ``.env`` and ``secrets.env``;
# this is the generalisation it was reaching for, and it is what closes
# ``*_creds.env`` and the private-key families.
_CREDENTIAL_SUFFIXES = (
    ".env",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jwt",
    ".kdbx",
    ".asc",
    ".ovpn",
)

# Names that carry credentials while matching no stem and no suffix.
_CREDENTIAL_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".htpasswd",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
    }
)


def _is_credential_component(part: str) -> bool:
    """Does one path component name a credential?

    Three anchored rules, in cost order. The stem rule matches the LAST word of
    each dot-separated segment, splitting on ``_`` and ``-``: that is what tells
    ``api-token`` and ``service_creds.env`` (credentials) apart from
    ``token-optimizer`` and ``analyze-token-usage.py`` (topics), because a
    credential file's name ENDS in the thing it holds while a topical one leads
    with it.

    Stated gap, so nobody reads this as a class guarantee: a credential whose
    name matches none of the three — ``token-store``, an ``.ini`` holding an
    API key, anything named for its service alone — is still served. This
    narrows a measured exposure; it does not close the category.
    """
    name = part.lower()
    if name in _CREDENTIAL_NAMES:
        return True
    if name.endswith(_CREDENTIAL_SUFFIXES):
        return True
    for segment in name.lstrip(".").split("."):
        if segment and re.split(r"[_\-]+", segment)[-1] in _CREDENTIAL_STEMS:
            return True
    return False


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
#: recognise is NOT covered. MEASURED by walking the three allowed roots and
#: reading the SQLite magic — **211 SQLite files, 22 of them uncovered**, and
#: that is a LOWER bound because the walk skipped ``.git``/``.venv`` and
#: swallowed permission errors. The managed browser profile's ``Cookies``,
#: ``History`` and ``Web Data`` are three of the 22, not the whole of it; an
#: earlier draft of this note said three, which understated the gap by a factor
#: of seven. A HARDLINK to the configured database defeats BOTH rules and is a
#: 23rd shape the enumeration cannot see at all.
#:
#: Closing that needs a registry of the databases the server actually opens,
#: which two independent reviewers converged on and which is tracked separately.
#: It is deliberately NOT approximated here with a third heuristic.
#:
#: Accepted cost, stated: an archived database copy matching a suffix is blocked
#: even though opening one cannot affect the live file's locks. These are binary
#: files a text route would mangle anyway.

#: Bound on the rename guard's directory walk. A rename is a rare, interactive
#: operation, so the cost only ever lands on a human who asked for it; the bound
#: exists so a pathological tree cannot stall a request thread, not to save time
#: on the common case.
_RENAME_SCAN_MAX_ENTRIES = 20_000

#: Latch so an unreadable database config warns ONCE per process rather than on
#: every request. Measured: 50 requests produced 50 warnings, each with a full
#: traceback — which buries the signal it exists to raise.
_identity_failure_logged = False

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


def _configured_db_identity() -> tuple[Path | None, Path | None]:
    """Resolve the configured database once: ``(db_path, area)``.

    ``area`` is the directory whose whole contents are refused, or None when
    directory-scoping would be too broad to apply (see below). ``db_path`` is
    None when config cannot be read at all.

    THIS TOUCHES THE FILESYSTEM. ``Path.resolve()`` is realpath — it walks and
    stats every component. An earlier revision of this module claimed the
    configured-area check was "pure path arithmetic"; MEASURED, it is 265.5 us
    per call against 30.3 us for the arithmetic alone. That is why the result is
    computed ONCE and handed to the rename walk rather than recomputed per
    child, where it cost ~5.3s at the 20,000-entry scan bound.

    DIRECTORY-SCOPING IS ONLY APPLIED WHERE IT IS MEANINGFUL: when the
    database's parent lies STRICTLY INSIDE an allowed root. Anywhere else,
    refusing "everything beside the database" refuses an entire allowed root:

    * ``~/genesis/genesis.db`` — the parent IS a root, so everything beside it
      is everything in that root.
    * ``~/genesis.db`` — the parent is an ANCESTOR of all three roots, so it
      denies all of them at once. Review (Devin) found this one after the first
      was fixed, which is the tell: the first fix answered the INSTANCE
      (``parent == root``) and left the class. The predicate below answers the
      class, so a third arrangement of the same shape cannot appear.

    Where scoping does not apply, ``area`` is None and the database's sidecars
    are covered by an ANCHORED name prefix instead.

    A RELATIVE configured path is resolved, not refused, and that is deliberate.
    ``genesis.env.genesis_db_path`` returns the configured value unresolved, and
    the server opens the database by resolving that same relative value from
    that same working directory — and THIS CODE RUNS IN THAT PROCESS. So
    ``resolve()`` here names the same file the server has open, by construction.
    An earlier revision of this function refused relative paths as "meaningless";
    review EXECUTED it and found the opposite: identity went ``(None, None)``,
    the name rule missed a database called ``store``, and ``_is_allowed``
    returned True FOR THE LIVE DATABASE — turning a 403 into a fail-open on the
    one file this module exists to protect.

    Failing to establish identity returns ``(None, None)`` and is LOGGED rather
    than silent. The name rule still runs behind it, so this declines to add a
    reason rather than opening anything — but an install whose config cannot be
    read is running with the authoritative half of this guard switched off, and
    that should not be discoverable only by reading the source.
    """
    global _identity_failure_logged
    try:
        from genesis.env import genesis_db_path

        db_path = genesis_db_path().resolve()
    except Exception:
        # resolve() is inside the try on purpose: it can raise OSError on a
        # symlink loop or an unreadable component, and an earlier revision left
        # it outside, so the documented "(None, None) on failure" contract
        # became a 500 out of every route.
        if not _identity_failure_logged:
            _identity_failure_logged = True
            logger.warning(
                "file routes: configured database path unreadable — the name "
                "rule is now the only database check, so a database under an "
                "unrecognised name is NOT protected. Logged once per process.",
                exc_info=True,
            )
        return (None, None)

    parent = db_path.parent
    strictly_inside_a_root = any(
        parent != root.resolve() and parent.is_relative_to(root.resolve())
        for root in _ALLOWED_ROOTS
    )
    area: Path | None = parent if strictly_inside_a_root else None
    return (db_path, area)


def _in_configured_database_area(
    resolved: Path, identity: tuple[Path | None, Path | None] | None = None
) -> bool:
    """Is *resolved* the configured database, or anything beside it?

    The one AUTHORITATIVE check here: the path comes from config, not from a
    guess about names, so it covers the main database under any spelling —
    including the extensionless form ``genesis.env`` explicitly supports — and
    every sidecar it keeps in the same directory, present or future.

    Directory-scoped where it can be. A database keeps company: ``-wal``,
    ``-shm``, ``-journal``, master journals, pre-restore copies. Enumerating
    those by name is the approach that kept coming up short, and a dedicated
    data directory holds nothing a file browser needs to open anyway.

    Pass *identity* to reuse one resolution across many calls; the rename walk
    does, because resolving per child is what made it slow.
    """
    db_path, area = identity if identity is not None else _configured_db_identity()
    if db_path is None:
        return False
    if resolved == db_path:
        return True
    if area is not None:
        return resolved.is_relative_to(area)
    # The database sits directly in an allowed root, so "the directory beside
    # it" is the whole root and cannot be refused wholesale. Cover its sidecars
    # by their own name — NOT a new guess about what a database looks like, but
    # the configured name plus whatever SQLite appends to it.
    #
    # ANCHORED on the separator, and that is not fussiness. A bare
    # `startswith(db_path.name)` matches every CONTINUATION of the name: with a
    # database called `x`, review EXECUTED it and found `xyz.txt`,
    # `xtra-notes.md` and `xylophone.py` all blocked — the same over-blocking
    # the directory exemption above exists to undo, reintroduced three lines
    # later and spread across a whole allowed root. Every real sidecar is
    # `<name>-wal`, `<name>-shm`, `<name>-journal`, `<name>-mj<hex>` or a
    # `<name>.`-prefixed copy, so anchoring loses none of them.
    if resolved.parent != db_path.parent:
        return False
    return resolved.name == db_path.name or resolved.name.startswith(
        (db_path.name + "-", db_path.name + ".")
    )


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

    Classifies children by NAME only, and deliberately does not open or read
    them. An earlier revision consulted ``/proc/self/fd`` per child — O(entries x
    descriptors), measured at ~6s for 5,000 files against 300 descriptors on a
    request thread — and then read each one's header, which meant a rename
    request could itself drop the server's locks. Walking a directory is not a
    reason to touch every file in it.

    The configured-database identity is resolved ONCE, before the walk, and
    reused for every child. Resolving it per child is realpath per child —
    MEASURED at 265.5 us, i.e. ~5.3s at the entry bound — which is the same
    per-child-filesystem-work mistake in a cheaper disguise, in the function
    whose own docstring condemns it.
    """
    identity = _configured_db_identity()
    seen = 0
    stack = [directory]
    while stack:
        current = stack.pop()
        # Iterate the scandir handle DIRECTLY rather than materialising it with
        # `list(it)` first. The bound below is what makes this safe on a hostile
        # tree, and a bound checked after the allocation is not a bound: review
        # (Devin) pointed out that a directory of a million entries built a
        # million `DirEntry` objects before the counter reached 20,001 and
        # refused. The OSError handler now wraps the ITERATION too, because
        # scandir can raise part-way through, not only at open.
        try:
            with os.scandir(current) as it:
                for entry in it:
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
                        Path(entry.path), identity
                    ):
                        return True
        except OSError:
            # Cannot inspect it, so cannot clear it. Refuse.
            return True
    return False


def _auth_or_403():
    """Return a 403 response tuple if not authenticated, else None.

    Mirrors ``references.py``. Every route in this module reaches the operator's
    filesystem — read, write, rename, delete, download, upload — and the module
    previously carried no auth check at all: the blueprint gate exempts
    ``/api/*`` and the app-level gate exempts GET, so a password-protected
    install served its filesystem to any anonymous caller that could reach the
    port.

    ``is_authenticated`` is deliberate, and it is the only credential predicate
    this module could use: it returns True when no password is configured, so
    this gate is INERT on a passwordless install and no operator loses access
    they had.

    What that leaves uncovered, stated plainly rather than implied away. The
    credential vocabulary in ``_is_allowed`` is a DISCLOSURE control — it
    governs which paths are addressable, so it narrows what a passwordless
    install hands out, and it does nothing whatever for the write routes. On an
    install with no password, ``write``/``create``/``rename``/``delete``/
    ``upload`` remain open to anyone who can reach the port, and an ordinary
    non-credential path is the one that matters there: ``~/.claude`` holds hook
    configuration, ``~/genesis`` holds the source the server imports. That
    exposure is a property of choosing not to configure a credential, it
    predates this gate, and nothing in this module can close it — the operator
    has declined to supply the thing a gate would check.
    """
    if not is_authenticated():
        return jsonify({"error": "authentication required"}), 403
    return None


def _is_allowed(path: Path) -> bool:
    """Check that *path* resolves inside an allowed root and isn't blocked.

    CONTAINMENT IS CHECKED FIRST, and that ordering is load-bearing rather than
    stylistic: every later check returns False on a match, so reordering cannot
    change the verdict — but it does change what this function TOUCHES. The
    directory test below stats the path, the configured-database check realpaths
    the configured value, and the rename guard walks the tree. Doing any of that
    to a path not yet proven to lie inside an allowed root is filesystem work on
    unvalidated input, which is what CodeQL's py/path-injection flagged here.
    Reject out-of-root paths before touching the filesystem at all.

    (An earlier revision of this docstring said "nothing here opens or STATS the
    caller's path". That was false — ``resolve()`` and ``is_dir()`` both stat it.
    The SAFETY argument survives, because a stat takes no POSIX lock and opens no
    descriptor, which is the property this guard actually needs; the absoluteness
    did not, and a reader who believed it would conclude the ordering was moot
    and reorder it.)
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
    # Same idea for credential-bearing names, anchored — see
    # ``_is_credential_component``. This half is what narrows DISCLOSURE on a
    # PASSWORDLESS install: the route gate reduces to a no-op there by design,
    # so without this the files remained readable on exactly the configuration
    # where they were found being served. It is not a substitute for that gate
    # and does nothing for the write routes — see ``_auth_or_403``.
    if any(_is_credential_component(part) for part in resolved.parts):
        return False
    # DIRECTORIES ARE EXEMPT from both database rules, and this is checked BEFORE
    # them. A directory cannot be opened as a database, so it cannot trigger the
    # lock loss this guards. Blocking one bought nothing and cost real function:
    # the configured data directory is itself "inside the configured database
    # area", so `file_list` returned 403 for it and the whole directory became
    # unbrowsable — every ordinary file in it too, though listing a directory
    # opens nothing. Review (Devin) reported it against the default path.
    #
    # What a directory can still reach is bounded by the callers, not by trust:
    # `file_delete` refuses directories outright, `file_read` and `file_download`
    # require `is_file()`, and `file_rename` has the recursive
    # `_contains_live_database` guard. So exempting one grants listing and
    # renaming-when-empty-of-databases, and nothing that opens a file.
    #
    # Stated limit: a not-yet-existing path cannot be told apart from a file
    # here, so creating a NEW directory with a database-shaped name, or one
    # inside the database area, is still refused. Narrow, and the caller can
    # rename into it afterwards.
    try:
        if resolved.is_dir():
            return True
    except OSError:
        pass
    # PRIMARY: the configured database and everything beside it. Authoritative
    # rather than heuristic — the path comes from config, so it covers the main
    # database under any name (including none) and every sidecar.
    if _in_configured_database_area(resolved):
        return False
    # SECONDARY: name, for databases living elsewhere. Checked on the RESOLVED
    # name, so a SYMLINK cannot smuggle one through under an innocent-looking
    # path. A HARDLINK still can: a second name for the same inode, created
    # outside these routes, is indistinguishable from an ordinary file by both
    # rules here — it defeats the name rule by being named anything, and the
    # configured-area rule by living anywhere. That needs the registry (#2235),
    # not another guess.
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
    if (resp := _auth_or_403()) is not None:
        return resp
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

    return jsonify(
        {
            "path": str(target),
            "parent": str(target.parent)
            if target != target.parent and target.resolve() != _HOME.resolve()
            else None,
            "entries": items,
        }
    )


@blueprint.route("/api/genesis/files/read")
def file_read():
    """Read a file's content.

    Query params:
        path – absolute file path
    """
    if (resp := _auth_or_403()) is not None:
        return resp
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
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".md": "markdown",
        ".html": "html",
        ".css": "css",
        ".sh": "sh",
        ".bash": "sh",
        ".toml": "toml",
        ".sql": "sql",
        ".xml": "xml",
    }

    return jsonify(
        {
            "path": str(target),
            "name": target.name,
            "content": content,
            "size": len(content),
            "mode": mode_map.get(suffix, "text"),
            "writable": os.access(target, os.W_OK),
        }
    )


@blueprint.route("/api/genesis/files/write", methods=["PUT"])
def file_write():
    """Write content to a file.

    JSON body: {path: str, content: str}
    """
    if (resp := _auth_or_403()) is not None:
        return resp
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
    if (resp := _auth_or_403()) is not None:
        return resp
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
    if (resp := _auth_or_403()) is not None:
        return resp
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
    if (resp := _auth_or_403()) is not None:
        return resp
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
    if (resp := _auth_or_403()) is not None:
        return resp
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
    if (resp := _auth_or_403()) is not None:
        return resp
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

    return jsonify(
        {
            "path": str(dest),
            "filename": rel_display,
            "size": file_size,
        }
    )
