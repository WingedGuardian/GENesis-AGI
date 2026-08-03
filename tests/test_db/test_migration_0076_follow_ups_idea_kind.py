"""Migration 0076 — add ``follow_ups.kind = 'idea'`` (WS-M PR-2 ideas lane).

Covers the legacy CHECK-rebuild (2-value → 3-value kind), row/index preservation,
the new value being accepted + a bogus value still rejected, fresh/migrated
column-order parity, idempotency, the double-application path (create_all_tables
then the runner), no-table no-op, and down() (idea→tabled reclassify + narrow).
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M76 = importlib.import_module("genesis.db.migrations.0076_follow_ups_idea_kind")

# follow_ups as it exists BEFORE this migration (kind CHECK = 2 values).
_LEGACY_DDL = """
    CREATE TABLE follow_ups (
        id               TEXT PRIMARY KEY,
        source           TEXT NOT NULL,
        source_session   TEXT,
        content          TEXT NOT NULL,
        reason           TEXT,
        strategy         TEXT NOT NULL CHECK (
            strategy IN ('scheduled_task', 'surplus_task', 'ego_judgment', 'user_input_needed')
        ),
        scheduled_at     TEXT,
        status           TEXT NOT NULL DEFAULT 'pending' CHECK (
            status IN ('pending', 'scheduled', 'in_progress', 'completed', 'failed', 'blocked')
        ),
        linked_task_id   TEXT,
        priority         TEXT NOT NULL DEFAULT 'medium' CHECK (
            priority IN ('low', 'medium', 'high', 'critical')
        ),
        created_at       TEXT NOT NULL,
        completed_at     TEXT,
        resolution_notes TEXT,
        blocked_reason   TEXT,
        escalated_to     TEXT,
        verified_at      TEXT,
        verification_notes TEXT,
        pinned           INTEGER NOT NULL DEFAULT 0,
        kind             TEXT NOT NULL DEFAULT 'follow_up' CHECK (
            kind IN ('follow_up', 'tabled')
        ),
        domain           TEXT CHECK (
            domain IN ('internal', 'user_world')
        ),
        goal_id          TEXT,
        dedup_key        TEXT,
        revisit_condition TEXT
    )
"""
_LEGACY_INDEXES = (
    "CREATE INDEX idx_follow_ups_status ON follow_ups(status)",
    "CREATE INDEX idx_follow_ups_scheduled ON follow_ups(scheduled_at)",
    "CREATE INDEX idx_follow_ups_source ON follow_ups(source)",
    "CREATE INDEX idx_follow_ups_linked_task ON follow_ups(linked_task_id)",
    "CREATE UNIQUE INDEX idx_follow_ups_dedup ON follow_ups(dedup_key) WHERE dedup_key IS NOT NULL",
)


async def _make_legacy_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await db.execute(_LEGACY_DDL)
    for stmt in _LEGACY_INDEXES:
        await db.execute(stmt)
    # A row per surviving kind + a dedup_key to prove the unique index carries over.
    await db.execute(
        "INSERT INTO follow_ups (id, source, content, strategy, created_at, kind, "
        "dedup_key) VALUES "
        "('a', 'inbox_evaluation', 'hot', 'surplus_task', 't0', 'follow_up', 'k1'),"
        "('b', 'inbox_evaluation', 'cold', 'surplus_task', 't0', 'tabled', 'k2')"
    )
    await db.commit()
    return db


async def _indexes(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='follow_ups' "
        "AND name NOT LIKE 'sqlite_%'"
    )
    return {r[0] for r in await cur.fetchall()}


async def _columns(db: aiosqlite.Connection) -> list[str]:
    cur = await db.execute("PRAGMA table_info(follow_ups)")
    return [r[1] for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_up_preserves_rows_and_indexes_and_widens_check():
    db = await _make_legacy_db()
    cols_before = await _columns(db)
    await M76.up(db)

    # Rows preserved.
    cur = await db.execute("SELECT id, kind FROM follow_ups ORDER BY id")
    assert [tuple(r) for r in await cur.fetchall()] == [
        ("a", "follow_up"),
        ("b", "tabled"),
    ]
    # 'idea' now accepted.
    await db.execute(
        "INSERT INTO follow_ups (id, source, content, strategy, created_at, kind) "
        "VALUES ('c', 'surplus_ideation', 'an idea', 'surplus_task', 't1', 'idea')"
    )
    # A bogus kind is still rejected.
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO follow_ups (id, source, content, strategy, created_at, kind) "
            "VALUES ('d', 's', 'x', 'surplus_task', 't1', 'nope')"
        )
    # All 5 indexes recreated, incl. the partial-unique dedup index.
    idx = await _indexes(db)
    assert idx == {
        "idx_follow_ups_status",
        "idx_follow_ups_scheduled",
        "idx_follow_ups_source",
        "idx_follow_ups_linked_task",
        "idx_follow_ups_dedup",
    }
    # dedup uniqueness still enforced.
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO follow_ups (id, source, content, strategy, created_at, "
            "dedup_key) VALUES ('e', 's', 'x', 'surplus_task', 't1', 'k1')"
        )
    # Column order unchanged (revisit_condition stays last).
    assert await _columns(db) == cols_before
    await db.close()


@pytest.mark.asyncio
async def test_up_idempotent():
    db = await _make_legacy_db()
    await M76.up(db)
    cols = await _columns(db)
    await M76.up(db)  # second run must be a guarded no-op
    assert await _columns(db) == cols
    cur = await db.execute("SELECT COUNT(*) FROM follow_ups")
    assert (await cur.fetchone())[0] == 2
    await db.close()


@pytest.mark.asyncio
async def test_up_no_table_noop():
    db = await aiosqlite.connect(":memory:")
    await M76.up(db)  # must not raise
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='follow_ups'"
    )
    assert await cur.fetchone() is None
    await db.close()


@pytest.mark.asyncio
async def test_column_order_parity_with_fresh_create():
    """A migrated legacy DB and a fresh create_all_tables build agree on the
    follow_ups column set + order."""
    from genesis.db.schema import create_all_tables

    legacy = await _make_legacy_db()
    await M76.up(legacy)
    migrated_cols = await _columns(legacy)
    await legacy.close()

    fresh = await aiosqlite.connect(":memory:")
    await create_all_tables(fresh)
    fresh_cols = await _columns(fresh)
    await fresh.close()

    assert migrated_cols == fresh_cols


@pytest.mark.asyncio
async def test_double_application_fresh_then_runner_is_noop():
    """Fresh create_all_tables already carries 'idea' → up() guards to a no-op."""
    from genesis.db.schema import create_all_tables

    db = await aiosqlite.connect(":memory:")
    await create_all_tables(db)
    cols = await _columns(db)
    await M76.up(db)  # canonical CREATE already has 'idea' → guard returns
    assert await _columns(db) == cols
    # 'idea' works on the fresh schema too.
    await db.execute(
        "INSERT INTO follow_ups (id, source, content, strategy, created_at, kind) "
        "VALUES ('c', 'surplus_ideation', 'idea', 'surplus_task', 't1', 'idea')"
    )
    await db.close()


@pytest.mark.asyncio
async def test_down_reclassifies_idea_and_narrows():
    db = await _make_legacy_db()
    await M76.up(db)
    await db.execute(
        "INSERT INTO follow_ups (id, source, content, strategy, created_at, kind) "
        "VALUES ('c', 'surplus_ideation', 'idea', 'surplus_task', 't1', 'idea')"
    )
    await db.commit()

    await M76.down(db)

    # idea row preserved, reclassified to tabled.
    cur = await db.execute("SELECT kind FROM follow_ups WHERE id='c'")
    assert (await cur.fetchone())[0] == "tabled"
    # 'idea' no longer accepted after narrowing.
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO follow_ups (id, source, content, strategy, created_at, kind) "
            "VALUES ('d', 's', 'x', 'surplus_task', 't1', 'idea')"
        )
    await db.close()
