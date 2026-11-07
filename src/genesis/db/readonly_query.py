"""Run one ad-hoc SQL query against a Genesis database, read-only by construction.

The read-only property is enforced by SQLite, never by inspecting the SQL
text. Three independent layers, each closing something the others leave open:

1. A ``mode=ro`` URI (:func:`genesis.db.integrity.ro_uri`). The pager refuses
   to write the main database file.
2. ``PRAGMA query_only=ON``. SQLite refuses every statement that would change
   any database the connection holds.
3. An allowlist authorizer (:func:`_make_authorizer`), installed LAST so the
   two pragmas above can still be set. It is the only layer that stops side
   effects OUTSIDE the main file: measured on SQLite 3.45 with layer 1 alone,
   ``VACUUM INTO 'out.db'`` writes a new database file and
   ``ATTACH 'file:x?mode=rwc'`` creates one. Both are denied here because
   their first authorizer action is ``SQLITE_ATTACH``.

Logical database read-only does not mean zero filesystem writes: SQLite can
create its own WAL sidecars (``-wal``/``-shm``) and temporary spill files. A
missing database is an error, never created.

Resource bounds: a cooperative deadline (setup, progress and row checks, default
:data:`DEFAULT_TIMEOUT_S`), a per-value cap (:data:`MAX_VALUE_BYTES`) and a
conservative retained-result budget (:data:`MAX_RESULT_BYTES`). These are not
a hard process deadline or RSS ceiling. The busy cap is installed once during
trusted setup; a later native wait can outlast the remaining deadline.

Unsupported: rtree virtual tables. The rtree module prepares write statements
on its shadow tables when the table is first used, so the authorizer refuses
any query that touches one. FTS5 works.

Text-level exemptions for the ``sqlite3`` shell were rejected for this job: the
shell has dot-commands and option parsing that a command string can smuggle
past any pattern. This module never runs a shell and never loads extensions.

Pure and synchronous on purpose, so an MCP tool can reuse
:func:`run_readonly_query` later; the CLI lives in :mod:`genesis.db.query_cli`.
"""

from __future__ import annotations

import math
import sqlite3
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from genesis.db.admission import assert_admitted
from genesis.db.integrity import ro_uri
from genesis.env import db_busy_timeout_ms

__all__ = [
    "DEFAULT_MAX_ROWS",
    "DEFAULT_TIMEOUT_S",
    "MAX_VALUE_BYTES",
    "MAX_RESULT_BYTES",
    "QueryResultTooLarge",
    "QueryResult",
    "QueryTimeout",
    "ReadOnlyViolation",
    "run_readonly_query",
]

DEFAULT_MAX_ROWS = 200

# Timeout policy: the failure mode this bounds is MEASURED. A runaway query
# (an unbounded `WITH RECURSIVE` under ORDER BY) ran until killed and spilled
# ~52 MB of temp file in 5 s under TMPDIR (this build's TEMP_STORE=1 keeps
# temp tables on disk). A Claude Code session's TMPDIR is the shared,
# quota-capped cc-tmp volume, so one such query can starve every session; it
# also pins a WAL read snapshot for as long as it runs, which blocks
# checkpoints on the live database. 60 s is far above any interactive read.
DEFAULT_TIMEOUT_S = 60.0
# VM instructions between deadline checks. Cheap (a monotonic clock read) and
# fine-grained enough to stop within milliseconds of the deadline.
_PROGRESS_STEPS = 1000

# Cap on any one string or blob. SQLite's default is 1e9 bytes, so
# `zeroblob(1e9)` / `randomblob(...)` could spike memory by a gigabyte.
MAX_VALUE_BYTES = 100_000_000
# Conservative rows/columns accounting, not total process/native/encoder memory.
MAX_RESULT_BYTES = 100_000_000

# `data_version` is allowed ONLY with no argument: FTS5 prepares
# `PRAGMA data_version` (arg None, measured on SQLite 3.45) to detect changes,
# so denying it refused every FTS5 query, and Genesis memory search is FTS5.
# Reading it changes nothing; setting it is refused like any other value.
_ARGLESS_PRAGMAS = frozenset({"data_version"})

# Introspection pragmas only. Each takes at most a table, index, or schema
# NAME as its argument (``PRAGMA table_info(t)`` and the table-valued form
# ``pragma_table_info('t')`` both reach the authorizer with ``arg2='t'``), and
# none of them changes state whatever that argument is. Every other pragma is
# denied, with or without a value.
_ALLOWED_PRAGMAS = frozenset(
    {
        "table_info",
        "table_xinfo",
        "index_list",
        "index_info",
        "index_xinfo",
        "foreign_key_list",
        "table_list",
    }
)

# SQL functions with a side effect. Checked against the function list of this
# build's SQLite (``pragma_function_list``, SQLite 3.45): ``load_extension``
# loads native code; ``fts3_tokenizer`` with two arguments registers a
# tokenizer from a raw pointer (this build sets ENABLE_FTS3_TOKENIZER, so the
# two-argument form is always on); ``sqlite_log`` writes to the error log;
# ``optimize`` is the FTS4 auxiliary function that merges index segments;
# ``fts5`` hands out an internal API pointer. ``readfile``, ``writefile`` and
# ``edit`` are sqlite3-shell functions absent from this build, listed so a
# build that does register them still refuses them.
_DENIED_FUNCTIONS = frozenset(
    {
        "load_extension",
        "fts3_tokenizer",
        "sqlite_log",
        "optimize",
        "fts5",
        "readfile",
        "writefile",
        "edit",
    }
)

_ACTION_NAMES = {
    getattr(sqlite3, f"SQLITE_{name}"): f"SQLITE_{name}"
    for name in [
        "CREATE_INDEX",
        "CREATE_TABLE",
        "CREATE_TEMP_INDEX",
        "CREATE_TEMP_TABLE",
        "CREATE_TEMP_TRIGGER",
        "CREATE_TEMP_VIEW",
        "CREATE_TRIGGER",
        "CREATE_VIEW",
        "DELETE",
        "DROP_INDEX",
        "DROP_TABLE",
        "DROP_TEMP_INDEX",
        "DROP_TEMP_TABLE",
        "DROP_TEMP_TRIGGER",
        "DROP_TEMP_VIEW",
        "DROP_TRIGGER",
        "DROP_VIEW",
        "INSERT",
        "PRAGMA",
        "READ",
        "SELECT",
        "TRANSACTION",
        "UPDATE",
        "ATTACH",
        "DETACH",
        "ALTER_TABLE",
        "REINDEX",
        "ANALYZE",
        "CREATE_VTABLE",
        "DROP_VTABLE",
        "FUNCTION",
        "SAVEPOINT",
        "RECURSIVE",
    ]
}


class ReadOnlyViolation(sqlite3.DatabaseError):
    """The statement was refused: it reached an operation outside the allowlist.

    ``action`` names the first authorizer action denied (``SQLITE_INSERT``,
    ``SQLITE_ATTACH``, ...), or is ``None`` when the refusal came from
    somewhere other than the authorizer (a second statement, or SQLite's own
    read-only layers).
    """

    def __init__(self, message: str, action: str | None = None) -> None:
        super().__init__(message)
        self.action = action


class QueryTimeout(sqlite3.OperationalError):
    """The query exceeded its cooperative execution deadline."""


class QueryResultTooLarge(sqlite3.DataError):
    """The retained rows and columns exceed the result accounting budget."""


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False


def _make_authorizer(denied: list[str], seen: list[int] | None = None):
    """An allowlist authorizer; the first denied action's name lands in *denied*.

    Every call appends to *seen* when given: a prepared statement that never
    consulted the authorizer had no statement in it (empty or comments only).
    """
    ok, deny = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY
    always = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE}

    def authorizer(action, arg1, arg2, _db_name, _trigger):
        if seen is not None:
            seen.append(action)
        if (
            action in always
            or (action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() not in _DENIED_FUNCTIONS)
            or (action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in _ALLOWED_PRAGMAS)
            or (
                action == sqlite3.SQLITE_PRAGMA
                and arg2 is None
                and (arg1 or "").lower() in _ARGLESS_PRAGMAS
            )
        ):
            return ok
        if not denied:
            denied.append(_ACTION_NAMES.get(action, f"action {action}"))
        return deny

    return authorizer


def run_readonly_query(
    path: str | Path,
    sql: str,
    params: Sequence = (),
    max_rows: int | None = DEFAULT_MAX_ROWS,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
) -> QueryResult:
    """Run ONE read-only statement and return its columns and rows.

    ``max_rows=None`` returns every row. Otherwise at most ``max_rows`` rows
    come back and ``truncated`` says whether more existed. ``timeout_s=None``
    (or 0) disables the wall-clock deadline.

    Raises :class:`ReadOnlyViolation` when the statement is refused,
    :class:`QueryTimeout` past the deadline, ``sqlite3.ProgrammingError``
    ("no statement") for empty or comment-only SQL,
    :class:`genesis.db.integrity.DatabaseIntegrityError` when the database is
    quarantined, and any other :class:`sqlite3.Error` unchanged (a missing
    file is an ``OperationalError`` and is never created).
    """
    deadline = time.monotonic() + timeout_s if timeout_s else None
    deadline_interrupted = False

    def expired() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    def check_deadline() -> None:
        if expired():
            raise QueryTimeout(f"aborted: exceeded {timeout_s:g}s")

    def progress() -> int:
        nonlocal deadline_interrupted
        deadline_interrupted = expired()
        return int(deadline_interrupted)

    def busy_budget_ms() -> int:
        configured = max(0, int(db_busy_timeout_ms()))
        if deadline is None:
            return configured
        check_deadline()
        return min(configured, math.ceil(max(0.0, deadline - time.monotonic()) * 1000))

    def check_result_budget(size: int) -> None:
        if size > MAX_RESULT_BYTES:
            raise QueryResultTooLarge(
                f"aborted: result exceeds retained-result budget ({MAX_RESULT_BYTES} bytes)"
            )

    check_deadline()
    db_path = Path(path).expanduser()
    assert_admitted(db_path)
    check_deadline()
    conn = None
    denied: list[str] = []
    seen: list[int] = []
    try:
        conn = sqlite3.connect(ro_uri(db_path), uri=True, timeout=busy_budget_ms() / 1000)
        check_deadline()
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION, False)
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        if deadline is not None:
            conn.set_progress_handler(progress, _PROGRESS_STEPS)
        # Trusted setup only: never remove the authorizer to refresh this cap.
        conn.execute(f"PRAGMA busy_timeout = {busy_budget_ms()}")
        check_deadline()
        conn.execute("PRAGMA query_only = ON")
        check_deadline()
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
        conn.set_authorizer(_make_authorizer(denied, seen))
        check_deadline()
        # execute(), never executescript(): it refuses a second statement.
        cur = conn.execute(sql, tuple(params))
        if not seen:
            raise sqlite3.ProgrammingError("no statement: the SQL is empty or only comments")
        check_deadline()
        columns = [d[0] for d in cur.description or ()]
        rows: list[tuple] = []
        retained = sys.getsizeof(rows) + sys.getsizeof(columns)
        retained += sum(sys.getsizeof(column) for column in columns)
        check_result_budget(retained)
        # Overcharge one one-element list per retained row rather than guessing
        # the interpreter's list growth. Shared scalar objects may be charged twice.
        list_allowance = sys.getsizeof([None])
        truncated = False
        while True:
            check_deadline()
            row = cur.fetchone()
            check_deadline()
            if row is None:
                break
            if max_rows is not None and len(rows) >= max_rows:
                truncated = True
                break  # The discarded lookahead is not retained-result memory.
            retained += list_allowance + sys.getsizeof(row)
            retained += sum(sys.getsizeof(value) for value in row)
            check_result_budget(retained)
            rows.append(row)
        return QueryResult(columns=columns, rows=rows, truncated=truncated)
    except sqlite3.ProgrammingError as exc:
        if "one statement" in str(exc):
            raise ReadOnlyViolation("refused: one statement only") from exc
        raise
    except sqlite3.DatabaseError as exc:
        if denied:
            raise ReadOnlyViolation(
                "refused: query touched an operation outside the read-only "
                f"allowlist (denied: {denied[0]})",
                denied[0],
            ) from exc
        primary = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        if deadline is not None and (
            (primary == sqlite3.SQLITE_INTERRUPT and deadline_interrupted)
            or (
                primary in {sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                and expired()
            )
        ):
            raise QueryTimeout(f"aborted: exceeded {timeout_s:g}s") from exc
        if isinstance(exc, sqlite3.OperationalError) and "readonly database" in str(exc):
            raise ReadOnlyViolation(f"refused: not read-only ({exc})") from exc
        raise
    finally:
        if conn is not None:
            conn.close()
