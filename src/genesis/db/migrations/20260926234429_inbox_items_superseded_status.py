"""Add ``inbox_items.status = 'superseded'`` and reclassify superseded rows.

A parked inbox row whose file snapshot was replaced before it dispatched is not
a failure: a newer drop carries a superset of its content. Such rows were
written as ``status='failed'`` with an ``approval_invalidated:`` marker, so they
dominated every "failed" count and hid real failures among them.

SQLite cannot ALTER a CHECK constraint, so widening the ``status`` CHECK needs a
full table rebuild: create ``inbox_items_new`` with the widened CHECK, copy every
row (``rowid`` included — ``get_all_known`` breaks ``created_at`` ties on rowid,
so renumbering would change which row supplies a file's known hash), drop the
original, rename into place, and recreate every index the live table carried
plus the canonical set.

Backfill: rows written by the three supersession writers, current and retired —
``approval_invalidated:superseded by newer modification`` (current),
``approval_invalidated:superseded by new inbox scan`` (the pre-idempotent-approval
writer, same meaning: a newer scan replaced a parked row; no current writer) and
``approval_invalidated:content changed`` (a changed file invalidating a parked
row) — move from ``failed`` to ``superseded``. ``error_message`` is kept for
audit. Other ``approval_invalidated:`` reasons (approval terminal, source file
deleted/vanished, content removed before retry) are left as ``failed``.

Self-contained (no genesis imports) and idempotent: the rebuild is skipped when
the live DDL already allows ``'superseded'`` (fresh DBs get it from the canonical
CREATE in ``db/schema/_tables.py``); the backfill is a guarded UPDATE that
matches nothing on a second run. A live column this rebuild target does not
declare RAISES rather than being dropped — the runner rolls the whole migration
back. No ``db.commit()`` — the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite

# Canonical inbox_items columns (must match _tables.py's CREATE); only the
# status CHECK differs between up() and down().
_COLUMNS_TEMPLATE = """
    id             TEXT PRIMARY KEY,
    file_path      TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ({status_values})
    ),
    batch_id       TEXT,
    response_path  TEXT,
    created_at     TEXT NOT NULL,
    processed_at   TEXT,
    error_message  TEXT,
    retry_count    INTEGER NOT NULL DEFAULT 0,
    evaluated_content TEXT,
    drop_id        TEXT,
    batch_items    TEXT
"""

_WIDE_STATUSES = "'pending', 'processing', 'completed', 'failed', 'superseded'"
_NARROW_STATUSES = "'pending', 'processing', 'completed', 'failed'"

_CANONICAL_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_inbox_items_status ON inbox_items(status)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_items_file_path ON inbox_items(file_path)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_items_batch_id ON inbox_items(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_items_drop ON inbox_items(drop_id)",
)

# The exact error_message values that mean "replaced by a newer snapshot", not
# "failed" — every value any writer (current or retired) has produced. Matched
# with equality, never LIKE: '_' in the prefix is a LIKE wildcard.
_SUPERSEDED_REASONS = (
    "approval_invalidated:superseded by newer modification",
    "approval_invalidated:superseded by new inbox scan",
    "approval_invalidated:content changed",
)


async def _live_ddl(db: aiosqlite.Connection) -> str | None:
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='inbox_items'"
    )
    row = await cursor.fetchone()
    return (row[0] or "") if row else None


async def _rebuild(db: aiosqlite.Connection, *, status_values: str) -> None:
    """Rebuild inbox_items with the given status CHECK, preserving rows/rowids/
    indexes/triggers/views.

    Follows SQLite's documented generalized ALTER TABLE procedure
    (lang_altertable.html, "Making Other Kinds Of Table Schema Changes"):
    create the new table under a temporary name, copy, drop, rename into place,
    then recreate the indexes, triggers and views the DROP removed or broke.
    The canonical schema has no view, trigger or foreign key touching
    inbox_items; every such object handled here is one an install added.
    """
    # Foreign keys: the procedure disables them BEFORE the transaction, which a
    # migration cannot do (the runner owns the transaction, and PRAGMA
    # foreign_keys is a no-op inside one). With them enabled, DROP TABLE runs an
    # implicit DELETE that fails on any child row, so refuse up front with a
    # message naming the referencing table rather than failing mid-rebuild.
    cursor = await db.execute("PRAGMA foreign_keys")
    if (await cursor.fetchone())[0]:
        cursor = await db.execute(
            "SELECT DISTINCT m.name FROM sqlite_master AS m, "
            "pragma_foreign_key_list(m.name) AS f "
            "WHERE m.type = 'table' AND lower(f.\"table\") = 'inbox_items'"
        )
        referencing = [r[0] for r in await cursor.fetchall()]
        if referencing:
            raise RuntimeError(
                f"inbox_items rebuild: table(s) {referencing} declare a FOREIGN KEY "
                "to inbox_items; with foreign_keys=ON the rebuild's DROP TABLE would "
                "fail. Apply this migration with foreign_keys=OFF."
            )

    # Capture every explicit index on the live table so none is lost to the
    # DROP (autoindexes have NULL sql and are recreated by the PRIMARY KEY), and
    # EVERY view and trigger in the schema: ALTER TABLE ... RENAME reparses the
    # whole schema, and a view or trigger naming inbox_items — directly, through
    # another view, or from a trigger attached to some other table — fails that
    # reparse while the table is absent. Dropping a view also drops its INSTEAD
    # OF triggers. Neither holds data, so all of them are dropped first and
    # recreated after the rename in creation order (views before triggers, since
    # a trigger may name a view).
    cursor = await db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL ORDER BY rowid"
    )
    live_views = list(await cursor.fetchall())
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name='inbox_items' AND sql IS NOT NULL"
    )
    live_indexes = [r[0] for r in await cursor.fetchall()]
    cursor = await db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL "
        "ORDER BY rowid"
    )
    live_triggers = list(await cursor.fetchall())

    await db.execute("DROP TABLE IF EXISTS inbox_items_new")
    await db.execute(
        f"CREATE TABLE inbox_items_new ({_COLUMNS_TEMPLATE.format(status_values=status_values)})"
    )

    # table_xinfo, not table_info: table_info omits generated columns, which
    # would let an install-added generated column pass the drift check and be
    # silently dropped. The rebuild target declares none, so any live one
    # (hidden flag 2/3) is reported as drift below.
    cursor = await db.execute("PRAGMA table_xinfo(inbox_items)")
    src_cols = [r[1] for r in await cursor.fetchall()]
    cursor = await db.execute("PRAGMA table_xinfo(inbox_items_new)")
    dst_cols = {r[1] for r in await cursor.fetchall()}
    dropped = [c for c in src_cols if c not in dst_cols]
    if dropped:
        raise RuntimeError(
            f"inbox_items rebuild drift: live column(s) {dropped} are not in the "
            "rebuild target; refusing to drop their data. Add them to "
            "_COLUMNS_TEMPLATE to match the canonical _tables.py DDL."
        )
    collist = ", ".join(c for c in src_cols if c in dst_cols)
    # Identifiers come from PRAGMA / in-repo literals, never user input.
    await db.execute(
        f"INSERT INTO inbox_items_new (rowid, {collist}) "  # noqa: S608
        f"SELECT rowid, {collist} FROM inbox_items"
    )

    for name, _sql in reversed(live_triggers):
        quoted = name.replace('"', '""')
        await db.execute(f'DROP TRIGGER IF EXISTS "{quoted}"')
    for name, _sql in reversed(live_views):
        quoted = name.replace('"', '""')
        await db.execute(f'DROP VIEW "{quoted}"')
    await db.execute("DROP TABLE inbox_items")
    await db.execute("ALTER TABLE inbox_items_new RENAME TO inbox_items")
    for stmt in live_indexes:
        await db.execute(stmt)
    for stmt in _CANONICAL_INDEXES:
        await db.execute(stmt)
    for _name, stmt in live_views:
        await db.execute(stmt)
    for _name, stmt in live_triggers:
        await db.execute(stmt)


async def up(db: aiosqlite.Connection) -> None:
    ddl = await _live_ddl(db)
    if ddl is None:
        return  # bare DB (runner unit tests) — nothing to alter
    if "'superseded'" not in ddl:
        await _rebuild(db, status_values=_WIDE_STATUSES)
    await db.execute(
        "UPDATE inbox_items SET status = 'superseded' "
        "WHERE status = 'failed' AND error_message IN (?, ?, ?)",
        _SUPERSEDED_REASONS,
    )


async def down(db: aiosqlite.Connection) -> None:
    ddl = await _live_ddl(db)
    if ddl is None or "'superseded'" not in ddl:
        return
    # Preserve data: superseded rows return to the pre-migration encoding.
    await db.execute("UPDATE inbox_items SET status = 'failed' WHERE status = 'superseded'")
    await _rebuild(db, status_values=_NARROW_STATUSES)
