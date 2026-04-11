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

import importlib
import logging
import re
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
        migrations = []
        for path in sorted(_MIGRATIONS_DIR.iterdir()):
            m = _MIGRATION_PATTERN.match(path.name)
            if m:
                mid = m.group(1)
                name = path.stem  # e.g., "0001_add_update_history"
                migrations.append((mid, name, path))
        return migrations

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

            t0 = time.monotonic()
            try:
                # Import the migration module
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

                results.append(MigrationResult(
                    id=mid, name=name, success=True, duration_ms=duration_ms,
                ))
                logger.info("Applied migration: %s (%dms)", name, duration_ms)

            except Exception as exc:
                duration_ms = int((time.monotonic() - t0) * 1000)
                # Rollback the failed migration's changes via explicit SQL.
                # Log rollback failures at ERROR — DB state is unknown if
                # rollback fails.
                try:
                    await self._db.execute("ROLLBACK")
                except Exception as rb_exc:
                    logger.error(
                        "Rollback FAILED after migration %s error — "
                        "database state is unknown: %s",
                        name, rb_exc, exc_info=True,
                    )

                results.append(MigrationResult(
                    id=mid, name=name, success=False,
                    duration_ms=duration_ms, error=str(exc),
                ))
                logger.error("Migration %s failed: %s", name, exc, exc_info=True)
                break  # Stop on first failure

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
