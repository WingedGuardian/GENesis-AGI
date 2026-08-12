"""Migration 0083 — MW-3 entity typing classes + card-materialization columns.

Two coupled schema changes on ``entities``, done in ONE table rebuild (SQLite
cannot ALTER a CHECK constraint):

  1. ``entity_type`` CHECK gains ``host``/``install``/``project`` — the MW-3
     §6.4 first-card classes (host = a machine, install = a Genesis deployment,
     project = a user project). The old 11-type set is a strict SUBSET of the
     new 14-type set, so no existing row can violate the new CHECK and the copy
     needs no dedup — UNIQUE(norm_name, entity_type) already held.
  2. Two card columns: ``summary_updated_at TEXT`` and ``summary_dirty INTEGER
     NOT NULL DEFAULT 0`` (B3 card-materialization state; NULL/0 = never carded
     = today's behavior). NOTHING reads/writes them until later MW-3 PRs.

Idempotent: re-running detects ``'host'`` already in the CHECK and exits. Fresh
installs get the new shape directly via ``_tables.py`` (this migration is a
no-op there). ALSO mirrored in ``_migrate_add_columns`` (schema_both_build_paths:
``create_all_tables`` runs that function but NOT this numbered runner).

Runner contract (see ``runner.py``): ``up()`` MUST NOT call ``db.commit()`` /
``BEGIN`` / ``executescript()`` — the runner owns the atomic transaction.
"""

from __future__ import annotations

import aiosqlite

# The complete new ``entities`` DDL — 14-type CHECK + 2 card columns. Kept in
# lockstep with the canonical CREATE in ``db/schema/_tables.py``.
_NEW_DDL = """
    CREATE TABLE entities_new (
        entity_id   TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        norm_name   TEXT NOT NULL,
        entity_type TEXT NOT NULL CHECK (entity_type IN (
            'code_file','code_symbol','pr','commit',
            'product','device','repo','subsystem','person','org','concept',
            'host','install','project'
        )),
        summary     TEXT,
        summary_updated_at TEXT,
        summary_dirty INTEGER NOT NULL DEFAULT 0,
        source      TEXT NOT NULL DEFAULT 'extracted',
        status      TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','merged','gone')),
        merged_into TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        UNIQUE (norm_name, entity_type)
    )
"""

# The canonical column set of the rebuild target (must match _NEW_DDL). The row
# copy is a runtime column-NAME intersection against this — so a legacy/drifted
# table with an extra column is not silently truncated: any live column absent
# here raises (drift guard), and the two card columns (dst-only) take DEFAULTs.
_NEW_COLUMNS = (
    "entity_id",
    "name",
    "norm_name",
    "entity_type",
    "summary",
    "summary_updated_at",
    "summary_dirty",
    "source",
    "status",
    "merged_into",
    "created_at",
    "updated_at",
)
# Full-migration signature — ALL new type values AND both card columns. Keying
# idempotency on any single token would let a partially-upgraded table skip.
_REQUIRED_TYPE_LITERALS = ("'host'", "'install'", "'project'")
_CARD_COLUMNS = ("summary_updated_at", "summary_dirty")


async def _entities_sql(db: aiosqlite.Connection) -> str | None:
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='entities'"
    )
    row = await cursor.fetchone()
    return (row[0] or "") if row else None


async def _entities_columns(db: aiosqlite.Connection) -> set[str]:
    cursor = await db.execute("PRAGMA table_info(entities)")
    return {r[1] for r in await cursor.fetchall()}


async def _is_fully_migrated(db: aiosqlite.Connection, sql: str) -> bool:
    """True only when BOTH the new type literals AND both card columns exist —
    a partial upgrade (e.g. CHECK rebuilt but columns missing) is NOT migrated."""
    if not all(t in sql for t in _REQUIRED_TYPE_LITERALS):
        return False
    cols = await _entities_columns(db)
    return all(c in cols for c in _CARD_COLUMNS)


async def up(db: aiosqlite.Connection) -> None:
    sql = await _entities_sql(db)
    if sql is None:
        return  # fresh DB → _tables.py already creates the new shape
    if await _is_fully_migrated(db, sql):
        return  # idempotent — fully migrated already

    live_cols = await _entities_columns(db)
    drift = live_cols - set(_NEW_COLUMNS)
    if drift:
        # Refuse rather than DROP the source and lose that column's data. The
        # runner's txn rolls the whole migration back on this raise.
        raise RuntimeError(
            f"0083: entities has column(s) {sorted(drift)} not in the rebuild "
            f"target; refusing to copy-and-drop their data. Add them to _NEW_DDL "
            f"to match the canonical _tables.py schema."
        )
    shared = [c for c in _NEW_COLUMNS if c in live_cols]
    collist = ", ".join(shared)
    await db.execute("DROP TABLE IF EXISTS entities_new")
    await db.execute(_NEW_DDL)
    await db.execute(
        f"INSERT INTO entities_new ({collist}) SELECT {collist} FROM entities"  # noqa: S608
    )
    await db.execute("DROP TABLE entities")
    await db.execute("ALTER TABLE entities_new RENAME TO entities")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(norm_name)")


async def down(db: aiosqlite.Connection) -> None:
    """Reverse to the 11-type CHECK, dropping the card columns (dev/testing).

    LOSSY if any host/install/project rows exist — they violate the old CHECK,
    so this refuses when they are present rather than silently dropping them.
    """
    sql = await _entities_sql(db)
    if sql is None:
        return
    if "'host'" not in sql:
        return  # already at the old shape
    cursor = await db.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_type IN ('host','install','project')"
    )
    if (await cursor.fetchone())[0] > 0:
        raise RuntimeError(
            "0083 down(): host/install/project rows exist; the old CHECK would "
            "reject them. Retype or delete them before reversing."
        )
    await db.execute("DROP TABLE IF EXISTS entities_old")
    await db.execute(
        """
        CREATE TABLE entities_old (
            entity_id   TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            norm_name   TEXT NOT NULL,
            entity_type TEXT NOT NULL CHECK (entity_type IN (
                'code_file','code_symbol','pr','commit',
                'product','device','repo','subsystem','person','org','concept'
            )),
            summary     TEXT,
            source      TEXT NOT NULL DEFAULT 'extracted',
            status      TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active','merged','gone')),
            merged_into TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            UNIQUE (norm_name, entity_type)
        )
        """
    )
    old_cols = (
        "entity_id, name, norm_name, entity_type, summary, source, "
        "status, merged_into, created_at, updated_at"
    )
    await db.execute(
        f"INSERT INTO entities_old ({old_cols}) "  # noqa: S608
        f"SELECT {old_cols} FROM entities"
    )
    await db.execute("DROP TABLE entities")
    await db.execute("ALTER TABLE entities_old RENAME TO entities")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(norm_name)")
