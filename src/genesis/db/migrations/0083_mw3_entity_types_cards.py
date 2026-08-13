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

import contextlib

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

# `DROP TABLE entities` auto-drops every secondary index and trigger SQLite owns
# for it; recreating only `idx_entities_norm` (below) silently loses any local
# one a fork added. And a VIEW referencing `entities` makes the RENAME itself
# fail ("error in view … no such table") under the default `legacy_alter_table`.
# So: capture indexes+triggers before the DROP and replay them after the RENAME,
# and rename under `PRAGMA legacy_alter_table=ON` so a dependent view survives
# (it is not dropped, and re-resolves once the renamed table reappears). This is
# SQLite's own documented table-rebuild procedure. Auto-indexes (UNIQUE/PK) have
# `sql IS NULL` and are recreated by the new DDL, so they are excluded.
_AUX_CAPTURE_SQL = (
    "SELECT sql FROM sqlite_master "
    "WHERE tbl_name='entities' AND type IN ('index','trigger') "
    "AND sql IS NOT NULL AND name != 'idx_entities_norm'"
)


async def _capture_entity_aux(db: aiosqlite.Connection) -> list[str]:
    cursor = await db.execute(_AUX_CAPTURE_SQL)
    return [r[0] for r in await cursor.fetchall()]


async def _replay_entity_aux(db: aiosqlite.Connection, captured: list[str]) -> None:
    # The objects were auto-dropped with the old table, so a plain CREATE
    # succeeds. A replay failure (e.g. an index on a column this rebuild
    # intentionally drops) raises inside the caller's txn/savepoint → the whole
    # rebuild rolls back loud rather than silently losing the object.
    for sql in captured:
        await db.execute(sql)


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
    a partial upgrade (e.g. CHECK rebuilt but columns missing) is NOT migrated.

    Name-presence (not column CONSTRAINTS) is sufficient here: summary_dirty is
    only ever created by this rebuild or the canonical _tables.py CREATE, both of
    which declare it ``NOT NULL DEFAULT 0`` — no code path adds it otherwise, so a
    name-present-but-mis-constrained state is unreachable (and were it reached, the
    NOT NULL column in the rebuild target would reject a NULL row on copy, failing
    loud rather than skipping silently)."""
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
    aux = await _capture_entity_aux(db)
    await db.execute("PRAGMA legacy_alter_table=ON")  # dependent views survive RENAME
    try:
        await db.execute("DROP TABLE IF EXISTS entities_new")
        await db.execute(_NEW_DDL)
        await db.execute(
            f"INSERT INTO entities_new ({collist}) SELECT {collist} FROM entities"  # noqa: S608
        )
        await db.execute("DROP TABLE entities")
        await db.execute("ALTER TABLE entities_new RENAME TO entities")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(norm_name)")
        await _replay_entity_aux(db, aux)
    finally:
        await db.execute("PRAGMA legacy_alter_table=OFF")


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
    # Drift guard (mirror up()): the down-target knows exactly the 10 old columns
    # plus the 2 card columns it intentionally drops. Any OTHER live column would
    # be silently discarded by a fixed projection — refuse instead, so a later/
    # locally-added column can't lose its data on the way down.
    _OLD_COLUMNS = (
        "entity_id",
        "name",
        "norm_name",
        "entity_type",
        "summary",
        "source",
        "status",
        "merged_into",
        "created_at",
        "updated_at",
    )
    live_cols = await _entities_columns(db)
    drift = live_cols - set(_OLD_COLUMNS) - set(_CARD_COLUMNS)
    if drift:
        raise RuntimeError(
            f"0083 down(): entities has column(s) {sorted(drift)} not in the old "
            f"shape and not among the card columns being dropped; refusing to "
            f"copy-and-drop their data."
        )
    copy_cols = ", ".join(c for c in _OLD_COLUMNS if c in live_cols)
    aux = await _capture_entity_aux(db)  # local indexes/triggers (excl. idx_entities_norm)
    # down() is a MANUAL dev/testing entrypoint — the runner only calls up(), so
    # there is NO enclosing runner txn here; own atomicity with a savepoint.
    await db.execute("SAVEPOINT entities_down_rebuild")
    try:
        await db.execute("PRAGMA legacy_alter_table=ON")  # dependent views survive RENAME
        await db.execute(
            f"INSERT INTO entities_old ({copy_cols}) "  # noqa: S608
            f"SELECT {copy_cols} FROM entities"
        )
        await db.execute("DROP TABLE entities")
        await db.execute("ALTER TABLE entities_old RENAME TO entities")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(norm_name)")
        # A replayed object referencing a card column this down() drops raises
        # here; the savepoint rollback below then restores the pre-down table
        # (loud refuse rather than a half-reversed schema).
        await _replay_entity_aux(db, aux)
        await db.execute("RELEASE entities_down_rebuild")
    except BaseException:
        with contextlib.suppress(Exception):
            await db.execute("ROLLBACK TO entities_down_rebuild")
            await db.execute("RELEASE entities_down_rebuild")
        raise
    finally:
        with contextlib.suppress(Exception):
            await db.execute("PRAGMA legacy_alter_table=OFF")
