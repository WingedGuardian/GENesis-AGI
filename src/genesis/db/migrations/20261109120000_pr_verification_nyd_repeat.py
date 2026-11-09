"""Record whether a not-yet-done attempt repeated the one before it.

One nullable column on ``pr_verifications``: ``nyd_repeat`` (INTEGER). 1 when a
``not-yet-done`` attempt names the same remaining steps (its ``not_yet_done``
gaps) as the previous attempt, which was also ``not-yet-done``; 0 when they
differ or the previous attempt was something else; NULL for every other verdict.
``record_attempt`` derives it from the stored and the new evidence document, so
no fingerprint is stored and a row recorded before this column existed is
compared on its next attempt.

The validation picker keeps a not-yet-done row first in line until an attempt
repeats the one before it; only then does the row wait the re-attempt window.
Before this column exists the picker applies no wait, exactly as before.

Additive, nullable, no backfill: an existing row reads NULL, meaning "no repeat
recorded". Unindexed, so not mirrored into ``_migrate_add_columns``. Column
order matches the canonical DDL in ``schema/_tables.py``. The runner owns commit
and rollback. Fresh installs already have the column.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pr_verifications'"
    )
    if await cursor.fetchone() is None:
        return  # fresh install: create_all_tables builds the full canonical shape
    cursor = await db.execute("PRAGMA table_info(pr_verifications)")
    if "nyd_repeat" not in {row[1] for row in await cursor.fetchall()}:
        await db.execute("ALTER TABLE pr_verifications ADD COLUMN nyd_repeat INTEGER")


async def down(db: aiosqlite.Connection) -> None:
    """No-op, deliberately: the column is nullable and harmless to keep, and
    DROP COLUMN would discard the repeat record a waiting row depends on."""
