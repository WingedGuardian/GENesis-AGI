"""Add ``cc_sessions.last_extracted_byte`` — incremental transcript-read resume.

The memory-extraction cycle re-read every extractable transcript from byte 0
each pass (only the PARSE was skipped below the line watermark), synchronously
on the event loop — a periodic multi-second block that starved recall (503s).
This column stores the byte offset of the START of line ``last_extracted_line``
so the reader can ``seek()`` and read only the delta.

Nullable, NO default: NULL = "never computed" → the reader falls back to a full
scan from byte 0 once, then populates it. No backfill — existing rows self-heal
on their next extraction cycle. Load-bearing INVARIANT (enforced by the reader +
extraction tests): if ``last_extracted_byte IS NOT NULL`` it is the byte start of
line ``last_extracted_line``; any inconsistency degrades to a one-cycle legacy
scan, never a content error.

Self-contained + idempotent: the ADD is PRAGMA-guarded (applies via the base
path in ``_migrations.py::_migrate_add_columns`` OR this standalone numbered
runner; whichever ran first, the guard skips). No commit — the runner owns the
transaction.

(Numbered 0080: 0078 is the highest present on disk; 0079 is claimed by the
in-flight MW-1 PR. The runner applies by per-id tracking and only duplicate
prefixes are fatal.)
"""

from __future__ import annotations

import aiosqlite


async def _add_column(db: aiosqlite.Connection, table: str, col: str, decl: str) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    if not await cursor.fetchone():
        return  # fresh DB — CREATE TABLE / base-path ALTER already carries the column
    cursor = await db.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in await cursor.fetchall()}
    if col not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


async def up(db: aiosqlite.Connection) -> None:
    await _add_column(db, "cc_sessions", "last_extracted_byte", "INTEGER")


async def down(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='cc_sessions'"
    )
    if not await cursor.fetchone():
        return
    cursor = await db.execute("PRAGMA table_info(cc_sessions)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "last_extracted_byte" in cols:
        await db.execute("ALTER TABLE cc_sessions DROP COLUMN last_extracted_byte")
