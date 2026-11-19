"""Add ``cc_sessions.lesson_built_through`` (#4807).

The whole-session lesson builder re-judged each session's whole transcript on
every extraction cycle with new messages. This column holds the ``tool_use_id``
of the last tool entry the builder has judged, so a later cycle can judge only
what follows it (``learning/procedural/build_segment.select_part``). It is
written by the extraction job with compare-and-set, so a second process running
a cycle cannot move it backwards.

Nullable, no default, no backfill: NULL means the builder has not judged the
session yet, and the next judgement starts from the beginning. Mirrored in
``_migrations.py::_migrate_add_columns`` for the base schema path and declared
last in the canonical DDL. Skips a database without the table, which the base
schema path owns.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='cc_sessions'"
    )
    if await cursor.fetchone() is None:
        return
    cursor = await db.execute("PRAGMA table_info(cc_sessions)")
    present = {row[1] for row in await cursor.fetchall()}
    if "lesson_built_through" not in present:
        await db.execute("ALTER TABLE cc_sessions ADD COLUMN lesson_built_through TEXT")
