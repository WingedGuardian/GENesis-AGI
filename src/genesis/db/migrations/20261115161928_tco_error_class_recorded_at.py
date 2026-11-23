"""Add ``tool_call_outcomes.error_class`` and ``recorded_at`` (#4722).

``error_class``: the transcript outcome writer (``learning/tool_outcomes.py``)
classifies every result with ``transcript_analytics.classify``, which reads the
full result text and the record's ``toolDenialKind``. The row keeps neither (its
snippet is 400 scrubbed characters), so the class cannot be recomputed later and
is stored as the writer saw it.

``recorded_at``: when the writer inserted the row. ``timestamp`` is the call's
own time, and a resumed session's first parse inserts calls made long before
the writer existed. The earliest surviving ``recorded_at`` is a conservative
lower bound on full coverage (the first insert, at or after deploy), which lets
a reader tell coverage from backfill. No DEFAULT on purpose: only the
transcript writer sets it, and the Stop-hook sensor's rows stay NULL.

Both nullable, no default, no backfill: success rows (for ``error_class``), the
Stop-hook sensor's Edit/Write rows and every row written before this migration
carry NULL. Mirrored in ``_migrations.py::_migrate_add_columns`` for the base
schema path; the canonical DDL declares them last, in this order, so fresh and
migrated column order match. Skips a database without the table, which the
base schema path owns.
"""

from __future__ import annotations

import aiosqlite

COLUMNS = ("error_class", "recorded_at")


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_call_outcomes'"
    )
    if await cursor.fetchone() is None:
        return
    cursor = await db.execute("PRAGMA table_info(tool_call_outcomes)")
    present = {row[1] for row in await cursor.fetchall()}
    for column in COLUMNS:
        if column not in present:
            await db.execute(f"ALTER TABLE tool_call_outcomes ADD COLUMN {column} TEXT")
