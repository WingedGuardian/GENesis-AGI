"""Add ``tool_call_outcomes.error_class``: the class of each failed call (#4722).

The transcript outcome writer (``learning/tool_outcomes.py``) classifies every
result with ``transcript_analytics.classify``, which reads the full result text
and the record's ``toolDenialKind``. The row keeps neither (its snippet is 400
scrubbed characters), so the class cannot be recomputed later and is stored as
the writer saw it.

Nullable, no default, no backfill: success rows, the Stop-hook sensor's
Edit/Write rows and every row written before this column carry NULL. Mirrored in
``_migrations.py::_migrate_add_columns`` for the base schema path; the canonical
DDL declares it last, so fresh and migrated column order match. Skips a
database without the table, which the base schema path owns.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_call_outcomes'"
    )
    if await cursor.fetchone() is None:
        return
    cursor = await db.execute("PRAGMA table_info(tool_call_outcomes)")
    if "error_class" not in {row[1] for row in await cursor.fetchall()}:
        await db.execute("ALTER TABLE tool_call_outcomes ADD COLUMN error_class TEXT")
