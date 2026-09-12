"""Human-approval gate + reversibility journal for entity merges (PR-1).

Two additions, both required to make the entity-adjudication apply path SAFE:

1. ``entity_adjudications.approved_at`` / ``approved_by`` — a proposed_merge is
   applied ONLY after a human sets ``approved_at``. The apply path
   (``_apply_proposed_backlog`` / ``apply_approved_merges``) filters on
   ``approved_at IS NOT NULL``, so flipping the drainer to ``live`` can never
   bulk-auto-apply the shadow backlog. NULL = unreviewed.

2. ``entity_merge_journal`` — a pre-delete snapshot of the loser's identity +
   mentions + links, written INSIDE ``merge_entity`` before its destructive
   DELETEs. ``merge_entity`` physically removes the loser's mention/link rows, so
   without this snapshot an applied merge is irreversible; the journal is the
   substrate for a future ``unmerge_entity``. Age-pruned by disk_hygiene.

Additive + idempotent (PRAGMA/duplicate-guarded). DDL mirrored in
``db/schema/_tables.py`` (the fresh-install path). Individual ``db.execute()``
calls, no commit — the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # -- entity_adjudications: approval columns --
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='entity_adjudications'"
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(entity_adjudications)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "approved_at" not in cols:
            await db.execute("ALTER TABLE entity_adjudications ADD COLUMN approved_at TEXT")
        if "approved_by" not in cols:
            await db.execute("ALTER TABLE entity_adjudications ADD COLUMN approved_by TEXT")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_entity_adjud_approved "
            "ON entity_adjudications(verdict, approved_at)"
        )

    # -- entity_merge_journal: reversibility snapshot --
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_merge_journal (
            id            TEXT PRIMARY KEY,
            loser_id      TEXT NOT NULL,
            survivor_id   TEXT NOT NULL,
            loser_name    TEXT,
            loser_norm    TEXT,
            loser_type    TEXT,
            mentions_json TEXT,
            links_json    TEXT,
            merged_at     TEXT NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_merge_journal_loser "
        "ON entity_merge_journal(loser_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_merge_journal_merged_at "
        "ON entity_merge_journal(merged_at)"
    )


async def down(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='entity_adjudications'"
    )
    if await cursor.fetchone():
        # Drop the index BEFORE the columns it references — SQLite refuses to drop
        # a column while an index depends on it.
        await db.execute("DROP INDEX IF EXISTS idx_entity_adjud_approved")
        cursor = await db.execute("PRAGMA table_info(entity_adjudications)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "approved_at" in cols:
            await db.execute("ALTER TABLE entity_adjudications DROP COLUMN approved_at")
        if "approved_by" in cols:
            await db.execute("ALTER TABLE entity_adjudications DROP COLUMN approved_by")
    await db.execute("DROP TABLE IF EXISTS entity_merge_journal")
