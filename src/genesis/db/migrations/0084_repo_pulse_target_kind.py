"""Add ``target_kind`` to repo_pulse_annotations + widen the dedupe index.

a8a4f59e — the repo-pulse annotator gains a second reconciliation lane for
standalone ``follow_ups`` rows alongside the existing ``session_ledger`` lane.
Every annotation now records WHICH store its ``item_id`` addresses:

  - ``target_kind`` — 'ledger' (existing rows backfill here via the DEFAULT) or
    'follow_up'. Ledger and follow_up ids are both uuid4.hex, so the re-absorb
    dedupe guard must scope by store: the UNIQUE index widens from
    ``(tier, item_id, pr_number)`` to ``(tier, target_kind, item_id, pr_number)``.
    Widening a unique index can only add distinctness, so existing (all-'ledger')
    rows can never violate the new constraint.

Additive + idempotent. The column ALTER is PRAGMA/duplicate-guarded; the index
is DROP-then-CREATE (SQLite can't ALTER an index). Mirrored in
``db/schema/_tables.py`` (CREATE carries the column; INDEXES carries the 4-col
unique index) and ``_migrate_add_columns`` (the create_all_tables base path),
per schema_both_build_paths. The runner owns the transaction — no commit here.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='repo_pulse_annotations'"
    )
    if not await cursor.fetchone():
        return  # fresh DB — create_all_tables already carries the column + 4-col index

    cursor = await db.execute("PRAGMA table_info(repo_pulse_annotations)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "target_kind" not in cols:
        await db.execute(
            "ALTER TABLE repo_pulse_annotations "
            "ADD COLUMN target_kind TEXT NOT NULL DEFAULT 'ledger'"
        )

    # Widen the re-absorb dedupe guard to scope by store (ledger vs follow_up
    # ids share the uuid4.hex shape). Drop the 3-col index, create the 4-col one.
    await db.execute("DROP INDEX IF EXISTS idx_rpa_dedupe")
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_rpa_dedupe "
        "ON repo_pulse_annotations(tier, target_kind, item_id, pr_number)"
    )


async def down(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='repo_pulse_annotations'"
    )
    if not await cursor.fetchone():
        return
    await db.execute("DROP INDEX IF EXISTS idx_rpa_dedupe")
    # The widened index admitted rows the narrow one can't: a ledger and a
    # follow_up annotation sharing (tier, item_id, pr_number) would violate the
    # recreated 3-col UNIQUE. The follow_up-target rows are THIS migration's own
    # data, so purge them (while the column still exists) before narrowing.
    await db.execute("DELETE FROM repo_pulse_annotations WHERE target_kind = 'follow_up'")
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_rpa_dedupe "
        "ON repo_pulse_annotations(tier, item_id, pr_number)"
    )
    cursor = await db.execute("PRAGMA table_info(repo_pulse_annotations)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "target_kind" in cols:
        await db.execute("ALTER TABLE repo_pulse_annotations DROP COLUMN target_kind")
