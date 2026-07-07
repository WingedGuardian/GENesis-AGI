"""Add ``task_states.source`` — who dispatched the task.

The dispatcher has always ACCEPTED a ``source`` argument (``user`` for /task
intake, internal names for observation dispatch) but never persisted it, so
delivery had no way to branch on provenance. The build lane needs exactly
that: ``source = 'build_lane'`` rows get the scope-gated diff check and the
server-side draft-PR open at delivery; every other source keeps today's
behavior byte-identical.

``NOT NULL DEFAULT 'user'`` — every pre-existing row WAS a user submission
(the only path that existed).

Fresh/test DBs get the column from the canonical CREATE TABLE in
``db/schema/_tables.py``; this numbered migration covers the existing-DB
upgrade path. Idempotent: PRAGMA-guarded ADD COLUMN. No commit — the runner
owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='task_states'"
    )
    if await cursor.fetchone():
        col_cursor = await db.execute("PRAGMA table_info(task_states)")
        cols = {row[1] for row in await col_cursor.fetchall()}
        if "source" not in cols:
            await db.execute(
                "ALTER TABLE task_states "
                "ADD COLUMN source TEXT NOT NULL DEFAULT 'user'"
            )
