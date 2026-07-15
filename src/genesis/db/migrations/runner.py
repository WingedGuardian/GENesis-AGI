"""Migration runner — discovers and applies versioned schema migrations.

Each migration is a Python file in this directory named NNNN_description.py
(e.g., 0001_add_update_history.py). Files must define:

    async def up(db: aiosqlite.Connection) -> None:
        '''Apply the migration. MUST NOT call db.commit() or db.rollback()
        — the runner commits atomically with the schema_migrations
        tracking row. This rule is enforced mechanically: the ``db``
        passed in is a proxy that raises RuntimeError on commit(),
        rollback(), or `async with db:`. All other connection methods
        (execute, executemany, fetchall, cursor, etc.) are forwarded
        unchanged.'''
        ...

Optional:
    async def down(db: aiosqlite.Connection) -> None:
        '''Reverse the migration (for development/testing).'''
        ...

Migrations run in filename order. Each migration body and its tracking
row are committed in a single atomic transaction — crash between up()
and tracking insert cannot leave the DB in a "applied but unrecorded"
state. On failure: rollback, stop, remaining migrations not attempted.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent
_MIGRATION_PATTERN = re.compile(r"^(\d{4})_\w+\.py$")

# SQL statements that would escape the runner's outer BEGIN IMMEDIATE /
# COMMIT / ROLLBACK envelope. Matched case-insensitively against the
# first token of the SQL passed to execute()/executemany().
_FORBIDDEN_SQL_TOKENS = frozenset({"BEGIN", "COMMIT", "ROLLBACK", "END", "SAVEPOINT", "RELEASE"})

# A migration's DDL can lose the lock to a concurrent reader holding the table
# schema. SQLite reports this as "database is locked" (SQLITE_LOCKED), which
# `busy_timeout` does NOT wait on — so a single-shot apply fails the moment a
# brief periodic writer (e.g. a ~5-min scheduler job) is mid-transaction. The
# runner instead retries the whole atomic apply (it was rolled back, so re-running
# is safe) with backoff until it catches a free window. Regression for the
# 2026-06-25 deploy incident (0037 lost the lock to the outreach scheduler).
_MAX_LOCK_RETRIES = 10
_LOCK_RETRY_BASE_DELAY_S = 2.0
_LOCK_RETRY_MAX_DELAY_S = 10.0


def _is_lock_error(exc: BaseException) -> bool:
    """True if ``exc`` is a transient SQLite lock-contention error.

    Covers both 'database is locked' (SQLITE_BUSY/SQLITE_LOCKED) and
    'database table is locked' — the retryable class. Anything else is a real
    migration bug and must fail fast (no retry).
    """
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def _first_sql_token(sql: object) -> str | None:
    """Return the UPPERCASE first token of a SQL string, or None.

    Non-string inputs (e.g. malformed calls) return None so the proxy
    can let the underlying connection raise its own TypeError.
    """
    if not isinstance(sql, str):
        return None
    stripped = sql.lstrip()
    if not stripped:
        return None
    # Split on the first whitespace or semicolon to isolate the verb.
    for i, ch in enumerate(stripped):
        if ch.isspace() or ch == ";":
            return stripped[:i].upper()
    return stripped.upper()


class _MigrationConnectionProxy:
    """Mechanically blocks transaction control inside a migration body.

    Migrations must never manage transactions — the runner wraps each
    migration body and its ``schema_migrations`` tracking row in a
    single explicit SQL ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK``
    envelope. Any attempt inside ``up()`` to commit, rollback, begin a
    sub-transaction, or enter/exit the connection as an async context
    manager breaks that atomicity and re-opens the DDL-rollback bug
    (Python sqlite3 auto-commits before DDL when the Python-level
    commit/rollback methods are used).

    This proxy enforces the rule at runtime through **two** layers:

    1. ``commit()``, ``rollback()``, ``__aenter__``, ``__aexit__`` raise
       ``RuntimeError`` — blocks the Python-method path.
    2. ``execute()`` and ``executemany()`` inspect the first SQL token
       and raise ``RuntimeError`` on ``BEGIN``, ``COMMIT``, ``ROLLBACK``,
       ``END``, ``SAVEPOINT``, ``RELEASE`` — blocks the raw-SQL escape
       hatch. Without this layer, a migration author could bypass the
       guard with ``await db.execute("COMMIT")`` and reopen the bug.

    All other attribute access delegates to the wrapped connection, so
    ``await db.fetchall(...)``, cursor acquisition, PRAGMA, DDL, DML,
    etc. continue to work normally.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    async def commit(self) -> None:
        raise RuntimeError(
            "Migration must not call db.commit() — the runner manages the "
            "transaction via explicit SQL COMMIT. Calling commit() inside "
            "up() breaks atomicity between the migration body and the "
            "schema_migrations tracking row, and re-opens the DDL-rollback "
            "bug in Python's sqlite3 driver. Remove the commit() call."
        )

    async def rollback(self) -> None:
        raise RuntimeError(
            "Migration must not call db.rollback() — the runner handles "
            "rollback via explicit SQL ROLLBACK when up() raises."
        )

    async def __aenter__(self):
        raise RuntimeError(
            "Migration must not use `async with db:` — aiosqlite's context "
            "manager commits on exit, which breaks the runner's atomic "
            "transaction. Use plain `await db.execute(...)` instead."
        )

    async def __aexit__(self, exc_type, exc, tb):
        raise RuntimeError(
            "Migration must not use `async with db:` — see __aenter__."
        )

    async def execute(self, sql: str, *args, **kwargs):
        token = _first_sql_token(sql)
        if token in _FORBIDDEN_SQL_TOKENS:
            raise RuntimeError(
                f"Migration must not execute `{token}` directly — the runner "
                f"owns the outer transaction. Offending SQL: {sql!r}. Remove "
                f"the transaction-control statement from up()."
            )
        return await self._conn.execute(sql, *args, **kwargs)

    async def executemany(self, sql: str, *args, **kwargs):
        token = _first_sql_token(sql)
        if token in _FORBIDDEN_SQL_TOKENS:
            raise RuntimeError(
                f"Migration must not executemany `{token}` — the runner "
                f"owns the outer transaction. Offending SQL: {sql!r}."
            )
        return await self._conn.executemany(sql, *args, **kwargs)

    async def executescript(self, sql_script: str, *args, **kwargs):
        # executescript is especially dangerous because it implicitly
        # COMMITs any pending transaction before executing. Block entirely.
        raise RuntimeError(
            "Migration must not call db.executescript() — it issues an "
            "implicit COMMIT before running the script, which breaks the "
            "runner's outer transaction. Use a sequence of db.execute() "
            "calls instead."
        )

    async def cursor(self, *args, **kwargs):
        # Block the direct cursor() escape hatch. A raw cursor would
        # bypass this proxy's execute filter entirely, letting a
        # migration author do `cur = await db.cursor(); await
        # cur.execute("COMMIT")`. Migrations don't need explicit
        # cursor() — `await db.execute(sql)` returns a cursor already.
        raise RuntimeError(
            "Migration must not call db.cursor() — this would bypass "
            "the transaction-control guard. Use `await db.execute(sql)`, "
            "which returns a cursor. For iteration, `async for row in "
            "await db.execute(sql):` works without explicit cursor()."
        )


@dataclass
class MigrationResult:
    id: str
    name: str
    success: bool
    duration_ms: int
    error: str | None = None


async def _ensure_tracking_table(db: aiosqlite.Connection) -> None:
    """Create schema_migrations table if it doesn't exist."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            duration_ms INTEGER
        )
    """)
    await db.commit()


class MigrationRunner:
    """Discovers and runs pending schema migrations."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def get_applied(self) -> set[str]:
        """Return set of applied migration IDs."""
        await _ensure_tracking_table(self._db)
        cursor = await self._db.execute("SELECT id FROM schema_migrations")
        rows = await cursor.fetchall()
        return {row[0] for row in rows}

    async def get_available(self) -> list[tuple[str, str, Path]]:
        """Return ordered list of (id, name, path) for all migration files."""
        # Shared file-discovery with the data-migration runner (only the
        # filename pattern differs; execution semantics are deliberately not).
        from genesis.db._migration_discovery import discover_numbered_modules

        return discover_numbered_modules(_MIGRATIONS_DIR, _MIGRATION_PATTERN)

    async def get_pending(self) -> list[tuple[str, str, Path]]:
        """Return ordered list of migrations not yet applied."""
        applied = await self.get_applied()
        available = await self.get_available()
        return [(mid, name, path) for mid, name, path in available if mid not in applied]

    async def run_pending(self, dry_run: bool = False) -> list[MigrationResult]:
        """Run all pending migrations in order. Returns results.

        Each migration runs in its own transaction. On failure: that
        migration's transaction is rolled back, execution stops, and
        the remaining migrations are not attempted.
        """
        # Pre-flight: detect duplicate migration prefixes before touching
        # the DB.  Two files sharing a prefix (e.g. 0008_foo and 0008_bar)
        # will cause a UNIQUE constraint failure on schema_migrations.id
        # mid-run — catch it here with a clear diagnostic instead.
        available = await self.get_available()
        seen: dict[str, str] = {}
        for mid, name, _path in available:
            if mid in seen:
                raise RuntimeError(
                    f"Duplicate migration prefix '{mid}': "
                    f"'{seen[mid]}' and '{name}'. "
                    f"Rename one file to use the next available prefix."
                )
            seen[mid] = name

        pending = await self.get_pending()
        if not pending:
            logger.info("No pending migrations")
            return []

        results: list[MigrationResult] = []
        for mid, name, path in pending:
            if dry_run:
                results.append(MigrationResult(id=mid, name=name, success=True, duration_ms=0))
                logger.info("[dry-run] Would apply: %s", name)
                continue

            # Apply with retry-on-lock. A migration's DDL can lose the lock to a
            # concurrent reader (SQLITE_LOCKED, which busy_timeout won't wait on).
            # Each attempt is a full atomic BEGIN..COMMIT, rolled back on failure,
            # so re-running is safe. The reconcile path (commit landed, then
            # SQLITE_BUSY) is checked BEFORE retrying, so a committed migration is
            # never re-run.
            result: MigrationResult | None = None
            for attempt in range(1, _MAX_LOCK_RETRIES + 1):
                t0 = time.monotonic()
                try:
                    # Import the migration module (cached; cheap to re-resolve).
                    spec_name = f"genesis.db.migrations.{path.stem}"
                    mod = importlib.import_module(spec_name)

                    if not hasattr(mod, "up"):
                        raise AttributeError(f"Migration {name} missing 'up' function")

                    # Atomic transaction: migration body + tracking row committed
                    # together via EXPLICIT SQL BEGIN IMMEDIATE / COMMIT / ROLLBACK.
                    # This is critical because Python's sqlite3 module auto-commits
                    # before DDL (CREATE/DROP) when using db.commit()/db.rollback()
                    # methods — explicit SQL transaction control bypasses that and
                    # ensures DDL is included in the rollback. Migration bodies
                    # MUST NOT call commit() or BEGIN themselves — enforced
                    # mechanically by _MigrationConnectionProxy, which raises
                    # RuntimeError on commit()/rollback()/async-with.
                    await self._db.execute("BEGIN IMMEDIATE")
                    await mod.up(_MigrationConnectionProxy(self._db))
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    await self._db.execute(
                        "INSERT INTO schema_migrations (id, name, applied_at, duration_ms) "
                        "VALUES (?, ?, ?, ?)",
                        (mid, name, datetime.now(UTC).isoformat(), duration_ms),
                    )
                    await self._db.execute("COMMIT")

                    result = MigrationResult(
                        id=mid, name=name, success=True, duration_ms=duration_ms,
                    )
                    logger.info("Applied migration: %s (%dms)", name, duration_ms)
                    break

                except Exception as exc:
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    # Roll back FIRST. A WAL COMMIT can return SQLITE_BUSY from a
                    # post-commit autocheckpoint AFTER the commit frame is durably
                    # written, so this error does NOT prove the migration failed.
                    # ROLLBACK is a harmless no-op ("no transaction is active") when
                    # the commit already landed, and undoes a genuinely open
                    # transaction otherwise — either way schema_migrations then holds
                    # the TRUE durable state.
                    try:
                        await self._db.execute("ROLLBACK")
                    except Exception as rb_exc:
                        logger.debug(
                            "ROLLBACK after migration %s error (expected when the "
                            "commit already landed): %s", name, rb_exc,
                        )

                    # Reconcile against the source of truth: is the migration durably
                    # recorded? If so, the COMMIT-time error was post-commit noise —
                    # treat it as applied instead of reporting a false failure (which
                    # previously rolled back the code and left new-schema + old-code).
                    # Checked BEFORE the retry decision so a committed migration is
                    # never re-run.
                    applied = False
                    try:
                        cur = await self._db.execute(
                            "SELECT 1 FROM schema_migrations WHERE id = ?", (mid,)
                        )
                        applied = await cur.fetchone() is not None
                    except Exception:
                        logger.error(
                            "Reconciliation read failed after migration %s error — "
                            "treating as failed", name, exc_info=True,
                        )
                        applied = False

                    if applied:
                        logger.warning(
                            "Migration %s raised %r but is durably recorded in "
                            "schema_migrations — treating as applied (likely a "
                            "post-commit WAL autocheckpoint SQLITE_BUSY).",
                            name, exc,
                        )
                        result = MigrationResult(
                            id=mid, name=name, success=True, duration_ms=duration_ms,
                        )
                        break

                    # Not applied. Retry transient lock contention (the migration
                    # was rolled back, so re-running is safe); fail fast otherwise.
                    if _is_lock_error(exc) and attempt < _MAX_LOCK_RETRIES:
                        delay = min(
                            _LOCK_RETRY_BASE_DELAY_S * attempt, _LOCK_RETRY_MAX_DELAY_S,
                        )
                        logger.warning(
                            "Migration %s hit lock contention (%s) — retry %d/%d "
                            "in %.1fs", name, exc, attempt, _MAX_LOCK_RETRIES, delay,
                        )
                        await asyncio.sleep(delay)
                        continue

                    result = MigrationResult(
                        id=mid, name=name, success=False,
                        duration_ms=duration_ms, error=str(exc),
                    )
                    if _is_lock_error(exc):
                        logger.error(
                            "Migration %s failed after %d lock-retry attempts: %s",
                            name, _MAX_LOCK_RETRIES, exc, exc_info=True,
                        )
                    else:
                        logger.error("Migration %s failed: %s", name, exc, exc_info=True)
                    break

            # `result` is always set: every path in the retry loop either breaks
            # (success / reconciled / terminal failure) or continues to retry, and
            # the final attempt cannot continue.
            assert result is not None  # noqa: S101 - type-narrowing invariant
            results.append(result)
            if not result.success:
                break  # Stop on first (terminal) failure

        return results

    async def status(self) -> dict:
        """Return summary of migration state."""
        applied = await self.get_applied()
        available = await self.get_available()
        pending = [(mid, name) for mid, name, _ in available if mid not in applied]

        return {
            "applied_count": len(applied),
            "pending_count": len(pending),
            "pending": [{"id": mid, "name": name} for mid, name in pending],
            "applied": sorted(applied),
        }
