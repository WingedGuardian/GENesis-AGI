"""reference_secrets.meta: a credential's caller metadata, held with its value (#2426).

A credential reference's value and description already live in
``reference_secrets``, out of every indexed store. The caller's own metadata
(the authority and provenance it passed, purpose, ingestion source,
relationships, caveats, and the identifier and tags as given) can repeat the
value in any spelling, and masking free text is unreliable, so it is held in the
same row as JSON (``meta``) and the unit carries neutral values instead.
Revealed with the value (``memory/reference_ops.reveal_meta``).

New-Store Gate: no new store; one nullable column on the existing table, with
its retention, consistency and backup (rides genesis.db). Additive and
idempotent; mirrored in ``db/schema/_tables.py`` and
``schema._migrations._migrate_add_columns`` (schema_both_build_paths). An
existing row reads NULL: no metadata held. The runner owns the transaction; no
commit here.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reference_secrets'"
    )
    if await cursor.fetchone() is None:
        return  # Pre-base path: create_all_tables builds the canonical shape.
    cursor = await db.execute("PRAGMA table_info(reference_secrets)")
    if "meta" not in {row[1] for row in await cursor.fetchall()}:
        await db.execute("ALTER TABLE reference_secrets ADD COLUMN meta TEXT")


async def down(db: aiosqlite.Connection) -> None:
    """The added column is left in place (SQLite drops columns only by rebuild)."""
