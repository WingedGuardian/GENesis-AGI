"""Add ``origin`` to ego_intentions — dispatch follow-through provenance.

B2b (dispatch follow-through): every terminal ego dispatch now creates a
system-origin intention forcing the owning ego to review the outcome on its
next cycle. The new column records who created a row:

  - ``origin`` — 'ego' (LLM-created via the intentions output channel; counts
    against ``MAX_ACTIVE_PER_SOURCE``) or 'system' (mechanical follow-through
    created by the dispatch on_end hook; bypasses the cap so follow-through is
    never silently dropped when the board is full). Existing rows are all
    LLM-created and backfill to 'ego' via the DEFAULT.

Additive + idempotent. The ALTER is PRAGMA/duplicate-guarded. Mirrored in
``db/schema/_tables.py`` (CREATE carries the column) and
``_migrate_add_columns`` (the create_all_tables base path), per
schema_both_build_paths. The runner owns the transaction — no commit here.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ego_intentions'"
    )
    if not await cursor.fetchone():
        return  # fresh DB — create_all_tables already carries the column

    cursor = await db.execute("PRAGMA table_info(ego_intentions)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "origin" not in cols:
        await db.execute("ALTER TABLE ego_intentions ADD COLUMN origin TEXT NOT NULL DEFAULT 'ego'")


async def down(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ego_intentions'"
    )
    if not await cursor.fetchone():
        return
    cursor = await db.execute("PRAGMA table_info(ego_intentions)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "origin" in cols:
        await db.execute("ALTER TABLE ego_intentions DROP COLUMN origin")
