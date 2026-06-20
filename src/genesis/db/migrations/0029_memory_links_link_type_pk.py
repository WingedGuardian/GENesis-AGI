"""Migration 0029 — add ``link_type`` to the ``memory_links`` primary key.

The original PK ``(source_id, target_id)`` allowed only ONE link between any
two memories, so a second link of a *different* type (e.g. ``contradicts``
after ``supports``, or ``succeeded_by``) was silently dropped on insert — the
writers (linker, connection_pass, dream_cycle) swallow the IntegrityError.
Audit finding DLI-04 / decision D15: the PK becomes
``(source_id, target_id, link_type)`` so distinct relationship types between
the same pair coexist.

SQLite can't ALTER a primary key, so this rebuilds the table. The OLD PK was
*stricter* than the new one (it already guaranteed one row per pair), so every
existing row is unique under the new PK — the copy needs no dedup
(``INSERT OR IGNORE`` is belt-and-suspenders). Idempotent: re-running detects
``link_type`` in the PK and exits.

Runner contract (see ``runner.py``): ``up()`` MUST NOT call ``db.commit()`` /
``BEGIN`` / ``executescript()`` / ``cursor()`` — the runner owns the atomic
transaction and a proxy raises ``RuntimeError`` on those. Fresh installs get the
new PK directly via ``_tables.py`` (this migration is a no-op there).
"""

from __future__ import annotations

import aiosqlite

_NEW_DDL = """
    CREATE TABLE memory_links_new (
        source_id   TEXT NOT NULL,
        target_id   TEXT NOT NULL,
        link_type   TEXT NOT NULL CHECK (
            link_type IN (
                'supports','contradicts','extends','elaborates',
                'discussed_in','evaluated_for','decided',
                'action_item_for','categorized_as','related_to',
                'succeeded_by','preceded_by'
            )
        ),
        strength    REAL NOT NULL DEFAULT 0.5,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (source_id, target_id, link_type)
    )
"""


async def _pk_columns(db: aiosqlite.Connection) -> list[str]:
    """Return memory_links PK column names in key order (empty if no table)."""
    cursor = await db.execute("PRAGMA table_info(memory_links)")
    rows = await cursor.fetchall()
    # PRAGMA columns: (cid, name, type, notnull, dflt_value, pk)
    # pk > 0 is the 1-based position within the primary key.
    pk = sorted((row[5], row[1]) for row in rows if row[5] and row[5] > 0)
    return [name for _, name in pk]


async def _table_exists(db: aiosqlite.Connection) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_links'"
    )
    return await cursor.fetchone() is not None


async def up(db: aiosqlite.Connection) -> None:
    # No table yet (fresh DB) → _tables.py already creates the new shape.
    if not await _table_exists(db):
        return
    # Already migrated → exit (idempotent).
    if "link_type" in await _pk_columns(db):
        return

    # Drop any leftover temp table from a prior interrupted run.
    await db.execute("DROP TABLE IF EXISTS memory_links_new")
    await db.execute(_NEW_DDL)
    await db.execute(
        "INSERT OR IGNORE INTO memory_links_new "
        "(source_id, target_id, link_type, strength, created_at) "
        "SELECT source_id, target_id, link_type, strength, created_at "
        "FROM memory_links"
    )
    await db.execute("DROP TABLE memory_links")
    await db.execute("ALTER TABLE memory_links_new RENAME TO memory_links")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_links_source "
        "ON memory_links(source_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_links_target "
        "ON memory_links(target_id)"
    )


async def down(db: aiosqlite.Connection) -> None:
    """Reverse to the ``(source_id, target_id)`` PK (development/testing only).

    LOSSY if multi-type pairs exist — ``INSERT OR IGNORE`` keeps one row per
    pair. The forward migration is the supported direction.
    """
    if not await _table_exists(db):
        return
    if "link_type" not in await _pk_columns(db):
        return
    await db.execute("DROP TABLE IF EXISTS memory_links_old")
    await db.execute(
        """
        CREATE TABLE memory_links_old (
            source_id   TEXT NOT NULL,
            target_id   TEXT NOT NULL,
            link_type   TEXT NOT NULL CHECK (
                link_type IN (
                    'supports','contradicts','extends','elaborates',
                    'discussed_in','evaluated_for','decided',
                    'action_item_for','categorized_as','related_to',
                    'succeeded_by','preceded_by'
                )
            ),
            strength    REAL NOT NULL DEFAULT 0.5,
            created_at  TEXT NOT NULL,
            PRIMARY KEY (source_id, target_id)
        )
        """
    )
    await db.execute(
        "INSERT OR IGNORE INTO memory_links_old "
        "(source_id, target_id, link_type, strength, created_at) "
        "SELECT source_id, target_id, link_type, strength, created_at "
        "FROM memory_links"
    )
    await db.execute("DROP TABLE memory_links")
    await db.execute("ALTER TABLE memory_links_old RENAME TO memory_links")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_links_source "
        "ON memory_links(source_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_links_target "
        "ON memory_links(target_id)"
    )
