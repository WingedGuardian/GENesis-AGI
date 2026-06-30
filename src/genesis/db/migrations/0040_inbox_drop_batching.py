"""Inbox URL-level batching: drop grouping + per-batch slice + follow-up dedup.

Adds:
- ``inbox_items.drop_id``    — groups the eval-batches carved from one file's
  delta so one approval covers the whole drop and resume can fan out the
  not-yet-completed batches.
- ``inbox_items.batch_items`` — the exact item lines a batch owns, persisted so
  resume re-dispatches the delta (not a full-file re-read) and survives restart.
- ``follow_ups.dedup_key``   — idempotent re-evaluation guard so the same
  recommendation does not pile duplicate follow-up rows.

Plus a non-unique index on ``inbox_items.drop_id`` and a PARTIAL unique index on
``follow_ups.dedup_key`` (partial so the many existing NULL-dedup_key rows do not
collide).

Fresh/test DBs get these from the canonical CREATE TABLE in
``db/schema/_tables.py`` + ``_migrate_add_columns`` in ``db/schema/_migrations.py``;
this numbered migration covers the existing-DB upgrade path via the runner.
Idempotent: PRAGMA-guarded column adds + ``IF NOT EXISTS`` indexes. No commit —
the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # inbox_items columns
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='inbox_items'"
    )
    if await cursor.fetchone():
        col_cursor = await db.execute("PRAGMA table_info(inbox_items)")
        cols = {row[1] for row in await col_cursor.fetchall()}
        if "drop_id" not in cols:
            await db.execute("ALTER TABLE inbox_items ADD COLUMN drop_id TEXT")
        if "batch_items" not in cols:
            await db.execute("ALTER TABLE inbox_items ADD COLUMN batch_items TEXT")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_inbox_items_drop "
            "ON inbox_items(drop_id)"
        )

    # follow_ups dedup_key + partial unique index
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='follow_ups'"
    )
    if await cursor.fetchone():
        col_cursor = await db.execute("PRAGMA table_info(follow_ups)")
        cols = {row[1] for row in await col_cursor.fetchall()}
        if "dedup_key" not in cols:
            await db.execute("ALTER TABLE follow_ups ADD COLUMN dedup_key TEXT")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_follow_ups_dedup "
            "ON follow_ups(dedup_key) WHERE dedup_key IS NOT NULL"
        )
