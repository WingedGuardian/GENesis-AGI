"""Add ``procedural_memory.created_session_id`` (#4717).

The lesson builder caps new lessons per source session over a rolling window,
counting rows by the session that created them. ``source`` names that session,
but a refine overwrites ``source`` (``learning/procedural/operations.py``), so a
refined lesson stopped counting against the session that made it. This column
is set by the insert statements in ``db/crud/procedural.py`` from the ``source``
JSON at insert time and never written again, so the count cannot change after
a lesson is created.

Nullable, no default, no backfill: rows written before this migration, and rows
whose ``source`` names no session (shipped lessons, seeds, explicit teaches),
carry NULL; the cap falls back to ``source`` for them. Mirrored in
``_migrations.py::_migrate_add_columns`` for the base schema path and declared in
the canonical DDL. Column order already differs between fresh and migrated
databases for this table (``extraction_context`` and ``first_mover`` are added by
ALTER only), and every insert names its columns. Skips a database without the
table, which the base schema path owns.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='procedural_memory'"
    )
    if await cursor.fetchone() is None:
        return
    cursor = await db.execute("PRAGMA table_info(procedural_memory)")
    present = {row[1] for row in await cursor.fetchall()}
    if "created_session_id" not in present:
        await db.execute("ALTER TABLE procedural_memory ADD COLUMN created_session_id TEXT")
