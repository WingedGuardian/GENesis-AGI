"""Rename ``procedural_memory.speculative`` -> ``draft``.

A procedure's unproven state lived in a column named ``speculative`` — but a
procedure isn't *speculating about* anything (the word Genesis reserves for
observations and hypothesis-claims); it is an untested **draft** destined for
real use. This renames ONLY the procedural_memory column so the vocabulary says
what it means. ``observations.speculative`` and ``speculative_claims.speculative``
are a genuinely different concept and are intentionally left as-is.

Pure column rename — no value rewrite. ``ALTER TABLE … RENAME COLUMN`` preserves
every row's data; SQLite auto-updates the column reference inside the existing
index, and we additionally rename the index *identifier*
(``idx_procedural_speculative`` -> ``idx_procedural_draft``) so it reads cleanly.

Idempotent: if the column is already ``draft`` (or ``speculative`` is gone), the
guard returns early, so applying twice — or against a fresh DB already born with
``draft`` from ``_tables.py`` — is a no-op. The code rename (CRUD params, query
literals, dict-key reads) ships in the same PR, so new rows are written against
``draft`` from the moment this lands.

Rollback path: revert the PR and apply a symmetric reverse rename; this file has
no ``down()`` (matches the migration-set norm).
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # The runner applies migrations against a bare DB in its own unit tests,
    # where base tables (created by `create_all_tables` in production) are
    # absent. Skip cleanly rather than fail the apply-all sequence. (Mirrors
    # 0035 / 0036.)
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='procedural_memory'"
    )
    if not await cursor.fetchone():
        return

    # Idempotency: skip if the rename already happened (column is ``draft``) or
    # there is nothing to rename (``speculative`` absent). Makes a re-run — or a
    # run against a fresh DB born with ``draft`` — a clean no-op.
    cursor = await db.execute("PRAGMA table_info(procedural_memory)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "draft" in columns or "speculative" not in columns:
        return

    # Rename the column (row data is preserved verbatim).
    await db.execute(
        "ALTER TABLE procedural_memory RENAME COLUMN speculative TO draft"
    )

    # Rename the index identifier to match. SQLite already repointed the old
    # index at the renamed column, so this is purely for a clean name.
    await db.execute("DROP INDEX IF EXISTS idx_procedural_speculative")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_procedural_draft "
        "ON procedural_memory(draft)"
    )
