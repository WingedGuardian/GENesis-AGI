"""Database admission — the single check every opener consults before connecting.

The 2026-09-18/19 corruption incident on a live install showed script-side raw
openers (audit hook, edit sensor, and friends) writing to a database the
integrity layer had already QUARANTINED: the central connectors refuse, but a
raw ``sqlite3.connect`` in ``scripts/`` consults nothing. This module is the
one predicate those openers call, and the one assertion the connection
factories make at open time.

WHAT THIS COVERS, AND WHAT IT DOES NOT
======================================

Covers: **quarantine** — "this artifact failed an integrity check"
(``genesis.db.integrity``), inode-bound by design, so a verified replacement
clears it naturally.

Does NOT cover: a *maintenance* fence — "an operator holds this path while
replacing the file". That was implemented here and then REMOVED rather than
shipped, and the reason is worth keeping. Two independent cross-model reviews
plus a syscall-level replay established that its reader and writer derived
keys separately and could disagree, and — the part that mattered — that the
perimeter it claimed did not exist. A check that binds some openers is useful;
one that *claims* to bind all of them is worse than none, because a recovery
procedure gets written against the claim. It returns with its write side
(``restore.sh``), a computed opener enumeration, and shared key derivation, so
the claim and the code can be checked against each other.

**So do not read this module as exclusion.** It removes the hook-writer class
that drove the recurrence. Exclusion during a replacement still comes from
stopping the services that hold the database.

KNOWN RESIDUAL, stated because a recovery operator needs it
-----------------------------------------------------------
Several modules under ``src/genesis/`` open the database directly rather than
through a factory, and are therefore bound only where they call one. The
read-WRITE offenders are fenced individually (the two MCP health tools, the
guardian watchdog's deploy-ref read, and the dashboard update route) because a
read-write open can checkpoint a stale WAL into the main file on close — the
recurrence mechanism itself. Roughly a dozen read-only opens remain unbound
and are enumerated in issue #2180; they hold a descriptor but cannot
checkpoint.

Fail direction: **closed**. Every consumer of :func:`database_is_fenced` is a
best-effort auxiliary reader/writer whose designed degrade is "skip the
database this once" — so an unreadable marker, a permission error, or any
unexpected failure reads as FENCED, never as clear. A skipped advisory write
is recoverable; a write to a quarantined database is the incident class.

Dependency rule: stdlib + ``genesis.env``/``genesis.db.integrity`` only (both
stdlib-clean), so CC hook processes can import this cheaply. Never import
``connection.py``, aiosqlite, or anything async here — the dependency direction
is ``connection -> admission``, one way.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from genesis.db.integrity import assert_not_quarantined, database_is_quarantined

__all__ = [
    "assert_admitted",
    "database_is_fenced",
]


def _coerce_path(db_path) -> Path:
    """Accept a filesystem path OR a SQLite ``file:`` URI; return the path.

    Load-bearing, not cosmetic: several script openers connect by URI
    (``file:...?mode=ro``), and
    :func:`genesis.db.integrity.database_is_quarantined` resolves whatever it
    is handed. Passing it the raw URI string yields a nonexistent path that
    matches no marker — silently un-fencing exactly the read-only callers that
    pass URIs. So the URI is parsed to its underlying path (query dropped,
    percent-encoding decoded) before any check.
    """
    # Only a STRING may be a URI. A `Path` is always a filesystem path, and
    # `file:literal.db` is a legal POSIX filename — stringifying a Path and
    # sniffing the prefix would check `literal.db` while the factory opened
    # the literal `file:literal.db`, so a quarantine marker on that real file
    # is missed and the seam is bypassed by a legal name.
    if isinstance(db_path, str) and db_path.startswith("file:"):
        parts = urlsplit(db_path)
        text = url2pathname(parts.path) if parts.path else parts.netloc
    else:
        text = str(db_path)
    return Path(text).expanduser()


def database_is_fenced(db_path) -> bool:
    """Predicate for best-effort openers: refuse to touch the database when True.

    Never raises. Callers are advisory readers and writers — audit trails,
    context injection, hooks — whose degrade is to skip the database for this
    one invocation, so a caller cannot be asked to handle an exception it has
    no better answer for than "skip". Any failure to establish state answers
    True.

    Use :func:`assert_admitted` instead wherever the caller is a real
    connection path, where silently continuing would mean opening the database
    this exists to protect.
    """
    try:
        path = _coerce_path(db_path)
    except Exception:
        return True
    try:
        return bool(database_is_quarantined(path))
    except Exception:
        # Includes the pathological legs: `database_is_quarantined` resolves
        # the path, and on CPython 3.12 `Path.resolve()` raises RuntimeError
        # (NOT OSError) for a symlink loop. The contract published here is
        # "never raises, fences on doubt" — keep that true at the boundary
        # rather than relying on every caller to carry its own guard.
        return True


def assert_admitted(path: str | Path) -> None:
    """Refuse a quarantined database. The assertion every factory makes.

    Called at OPEN time by the connection factories in
    :mod:`genesis.db.connection`. Quarantine is delegated unchanged — same
    predicate, same exception, same message — so this is one named seam rather
    than a reinterpretation of any existing refusal, and a maintenance fence
    can later be added here without touching six call sites again.

    Unlike :func:`database_is_fenced` this RAISES, because its callers are the
    real connection paths.
    """
    assert_not_quarantined(_coerce_path(path))
