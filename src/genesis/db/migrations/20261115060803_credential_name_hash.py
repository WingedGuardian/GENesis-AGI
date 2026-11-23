"""knowledge_units.name_hash and name_origin: a credential's identity (#2426).

A credential reference is stored under an opaque key (``cred-`` and 12 hex)
and found by the sha256 of its normalised full name
(``memory.reference_ops.name_hash``); only the hash is stored, never a second
copy of the name. ``name_origin`` is ``trusted``, ``untrusted`` (insert-only)
or ``carded`` (a name that had a card number redacted, whose hash no longer
tells two names apart). A partial unique index keeps one trusted entry per name.

Existing credential rows are backfilled from their recorded name
(``reference_ops.legacy_identity``: an opaque key's body header, else the key
itself, which was the identifier). Of several trusted rows whose names differ
only in case or spacing, the first created gets the hash; each later one gets
the hash with ``#later`` appended, a value the index leaves out (it covers only
64-character hashes) and no name hashes to. The later entry is still stored,
searchable, revealable and deletable, only never chosen by a write by name.
Idempotent: only rows with no hash are filled. Nothing depends on the backfill
having run first: the chokepoint finds a row with no hash by its recorded name
and gives it the hash on its next write by name.

New-Store Gate: no new store; two nullable columns on the existing table, which
ride genesis.db's backup. Mirrored in ``db/schema/_tables.py`` and
``schema._migrations._migrate_add_columns``. The runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite

COLUMNS = ("name_hash", "name_origin")
INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_units_name_hash "
    "ON knowledge_units(project_type, domain, name_hash) "
    "WHERE name_origin = 'trusted' AND length(name_hash) = 64"
)
#: Appended to the hash of a later trusted name that differs from an earlier
#: one only in case or spacing: out of the index, and matched by no name.
LATER = "#later"


async def up(db: aiosqlite.Connection) -> None:
    from genesis.memory.reference_ops import TRUSTED, legacy_identity

    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_units'"
    )
    if await cursor.fetchone() is None:
        return  # Pre-base path: create_all_tables builds the canonical shape.
    cursor = await db.execute("PRAGMA table_info(knowledge_units)")
    present = {row[1] for row in await cursor.fetchall()}
    for column in COLUMNS:
        if column not in present:
            await db.execute(f"ALTER TABLE knowledge_units ADD COLUMN {column} TEXT")
    cursor = await db.execute(
        "SELECT project_type, domain, name_hash FROM knowledge_units WHERE name_origin = ?",
        (TRUSTED,),
    )
    taken = {tuple(row) for row in await cursor.fetchall()}
    cursor = await db.execute(
        "SELECT rowid, project_type, domain, concept, body FROM knowledge_units "
        "WHERE lower(project_type) = 'reference' AND lower(domain) = 'reference.credentials' "
        "AND name_hash IS NULL ORDER BY rowid"
    )
    for rowid, project_type, domain, concept, body in await cursor.fetchall():
        name_hash, origin = legacy_identity(concept, body)
        if origin == TRUSTED:
            if (project_type, domain, name_hash) in taken:
                name_hash += LATER
            taken.add((project_type, domain, name_hash))
        await db.execute(
            "UPDATE knowledge_units SET name_hash = ?, name_origin = ? WHERE rowid = ?",
            (name_hash, origin, rowid),
        )
    await db.execute(INDEX)


async def down(db: aiosqlite.Connection) -> None:
    """The added columns are left in place (SQLite drops columns only by rebuild)."""
