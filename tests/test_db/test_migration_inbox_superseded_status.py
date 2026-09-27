"""Migration 20260926234429 — ``inbox_items.status = 'superseded'``.

Covers the CHECK-widening table rebuild (rows, rowids and every index kept), the
backfill (exactly the supersession markers move; other failures stay failed),
idempotency, the fresh-DB path (create_all_tables then the migration), a legacy
table missing later columns, the no-table no-op, and down().
"""

from __future__ import annotations

import importlib
import sqlite3

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables

M = importlib.import_module("genesis.db.migrations.20260926234429_inbox_items_superseded_status")

# inbox_items as a long-lived install has it: base CREATE plus ALTER-added columns.
_LEGACY_DDL = """
    CREATE TABLE inbox_items (
        id             TEXT PRIMARY KEY,
        file_path      TEXT NOT NULL,
        content_hash   TEXT NOT NULL,
        status         TEXT NOT NULL DEFAULT 'pending' CHECK (
            status IN ('pending', 'processing', 'completed', 'failed')
        ),
        batch_id       TEXT,
        response_path  TEXT,
        created_at     TEXT NOT NULL,
        processed_at   TEXT,
        error_message  TEXT
    , retry_count INTEGER NOT NULL DEFAULT 0, evaluated_content TEXT,
      drop_id TEXT, batch_items TEXT)
"""

_LEGACY_INDEXES = (
    "CREATE INDEX idx_inbox_items_status ON inbox_items(status)",
    "CREATE INDEX idx_inbox_items_file_path ON inbox_items(file_path)",
    "CREATE INDEX idx_inbox_items_batch_id ON inbox_items(batch_id)",
    "CREATE INDEX idx_inbox_items_drop ON inbox_items(drop_id)",
    # A non-canonical index an install may carry — must survive the rebuild.
    "CREATE INDEX idx_inbox_items_extra ON inbox_items(created_at, status)",
)

# (id, status, error_message, retry_count) -> expected status after up()
_ROWS = [
    ("a", "failed", "approval_invalidated:superseded by newer modification", 0, "superseded"),
    ("b", "failed", "approval_invalidated:superseded by new inbox scan", 1, "superseded"),
    ("c", "failed", "approval_invalidated:content changed", 2, "superseded"),
    ("d", "failed", "approval_invalidated:approval terminal:cancelled", 0, "failed"),
    ("e", "failed", "approval_invalidated:source file deleted", 0, "failed"),
    ("f", "failed", "approval_invalidated:content removed before retry", 0, "failed"),
    ("g", "failed", "CC invocation failed: timeout", 1, "failed"),
    ("h", "failed", None, 0, "failed"),
    ("i", "completed", "approval_invalidated:superseded by newer modification", 0, "completed"),
    ("j", "pending", None, 0, "pending"),
    ("k", "processing", "awaiting_approval:req-1", 0, "processing"),
]


async def _make_legacy_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await db.execute(_LEGACY_DDL)
    for stmt in _LEGACY_INDEXES:
        await db.execute(stmt)
    # Insert in an order that makes rowid differ from id order, so a renumbering
    # copy would be detectable.
    for n, (rid, status, err, retry, _exp) in enumerate(reversed(_ROWS)):
        await db.execute(
            "INSERT INTO inbox_items (rowid, id, file_path, content_hash, status, "
            "created_at, error_message, retry_count, drop_id, batch_items) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                100 + n * 7,
                rid,
                f"/inbox/{rid}.md",
                f"h-{rid}",
                status,
                "2026-09-01T00:00:00+00:00",
                err,
                retry,
                f"D-{rid}",
                f"item {rid}",
            ),
        )
    await db.commit()
    return db


async def _snapshot(db):
    cur = await db.execute(
        "SELECT rowid, id, file_path, content_hash, created_at, error_message, "
        "retry_count, drop_id, batch_items FROM inbox_items ORDER BY id"
    )
    return [tuple(r) for r in await cur.fetchall()]


async def _index_sql(db):
    cur = await db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name='inbox_items' AND sql IS NOT NULL ORDER BY name"
    )
    return {r[0]: r[1] for r in await cur.fetchall()}


async def _statuses(db):
    cur = await db.execute("SELECT id, status FROM inbox_items ORDER BY id")
    return {r[0]: r[1] for r in await cur.fetchall()}


@pytest.mark.asyncio
async def test_up_rebuilds_preserves_rows_rowids_indexes_and_backfills():
    db = await _make_legacy_db()
    before = await _snapshot(db)
    idx_before = await _index_sql(db)

    await M.up(db)

    assert await _snapshot(db) == before  # every column, and rowid, preserved
    assert await _index_sql(db) == idx_before  # every index, verbatim
    assert await _statuses(db) == {r[0]: r[4] for r in _ROWS}
    # The widened CHECK accepts the new value and still rejects junk.
    await db.execute("UPDATE inbox_items SET status = 'superseded' WHERE id = 'h'")
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute("UPDATE inbox_items SET status = 'bogus' WHERE id = 'h'")
    cur = await db.execute("SELECT name FROM sqlite_master WHERE name='inbox_items_new'")
    assert await cur.fetchone() is None
    await db.close()


@pytest.mark.asyncio
async def test_up_idempotent():
    db = await _make_legacy_db()
    await M.up(db)
    first = (await _snapshot(db), await _statuses(db), await _index_sql(db))
    await M.up(db)
    assert (await _snapshot(db), await _statuses(db), await _index_sql(db)) == first
    await db.close()


@pytest.mark.asyncio
async def test_fresh_db_has_superseded_and_up_is_safe():
    """create_all_tables already carries the widened CHECK; the migration then
    skips the rebuild but still backfills any matching rows."""
    db = await aiosqlite.connect(":memory:")
    await create_all_tables(db)
    await db.execute(
        "INSERT INTO inbox_items (id, file_path, content_hash, status, created_at, "
        "error_message) VALUES ('x', '/f', 'h', 'failed', 't', "
        "'approval_invalidated:content changed')"
    )
    idx_before = await _index_sql(db)
    await M.up(db)
    assert await _statuses(db) == {"x": "superseded"}
    assert await _index_sql(db) == idx_before
    await db.close()


@pytest.mark.asyncio
async def test_canonical_ddl_and_rebuild_target_agree_on_columns():
    """The rebuild target must declare every canonical column, in order, or a
    fresh and a migrated install diverge."""
    fresh = await aiosqlite.connect(":memory:")
    await create_all_tables(fresh)
    migrated = await _make_legacy_db()
    await M.up(migrated)
    cols = []
    for conn in (fresh, migrated):
        cur = await conn.execute("PRAGMA table_info(inbox_items)")
        cols.append([(r[1], r[2], r[3], r[4]) for r in await cur.fetchall()])
    assert cols[0] == cols[1]
    await fresh.close()
    await migrated.close()


@pytest.mark.asyncio
async def test_legacy_table_missing_later_columns_takes_defaults():
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        "CREATE TABLE inbox_items (id TEXT PRIMARY KEY, file_path TEXT NOT NULL, "
        "content_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK "
        "(status IN ('pending', 'processing', 'completed', 'failed')), batch_id TEXT, "
        "response_path TEXT, created_at TEXT NOT NULL, processed_at TEXT, "
        "error_message TEXT)"
    )
    await db.execute(
        "INSERT INTO inbox_items (id, file_path, content_hash, status, created_at, "
        "error_message) VALUES ('a', '/f', 'h', 'failed', 't', "
        "'approval_invalidated:superseded by new inbox scan')"
    )
    await M.up(db)
    cur = await db.execute("SELECT status, retry_count, drop_id FROM inbox_items")
    assert tuple(await cur.fetchone()) == ("superseded", 0, None)
    await db.close()


@pytest.mark.asyncio
async def test_install_local_trigger_survives_rebuild():
    """DROP TABLE drops attached triggers; the rebuild must replay them."""
    db = await _make_legacy_db()
    await db.execute("CREATE TABLE inbox_audit (id TEXT, status TEXT)")
    await db.execute(
        "CREATE TRIGGER trg_inbox_audit AFTER UPDATE OF status ON inbox_items "
        "BEGIN INSERT INTO inbox_audit VALUES (NEW.id, NEW.status); END"
    )
    await M.up(db)
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='inbox_items'"
    )
    assert [r[0] for r in await cur.fetchall()] == ["trg_inbox_audit"]
    # It still fires on the rebuilt table (the backfill ran after the replay).
    cur = await db.execute("SELECT id FROM inbox_audit WHERE status='superseded' ORDER BY id")
    assert [r[0] for r in await cur.fetchall()] == ["a", "b", "c"]
    await db.close()


@pytest.mark.asyncio
async def test_unknown_live_column_raises_instead_of_dropping_data():
    db = await _make_legacy_db()
    await db.execute("ALTER TABLE inbox_items ADD COLUMN future_col TEXT")
    with pytest.raises(RuntimeError, match="future_col"):
        await M.up(db)
    await db.close()


@pytest.mark.asyncio
async def test_up_no_table_noop():
    db = await aiosqlite.connect(":memory:")
    await M.up(db)
    await M.down(db)
    await db.close()


@pytest.mark.asyncio
async def test_down_restores_failed_and_narrow_check():
    db = await _make_legacy_db()
    before = await _snapshot(db)
    await M.up(db)
    await M.down(db)
    assert await _snapshot(db) == before
    assert set((await _statuses(db)).values()) == {"failed", "completed", "pending", "processing"}
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute("UPDATE inbox_items SET status = 'superseded' WHERE id = 'a'")
    await M.down(db)  # already narrowed — no-op
    await db.close()
