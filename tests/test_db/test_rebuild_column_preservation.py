"""Regression guard for the table-rebuild data-loss class (PR-4 unit C/D).

A boot-path "table rebuild" migration (SQLite can't ALTER a CHECK/UNIQUE, so it
CREATEs a new table, copies rows, DROPs the original, RENAMEs) silently drops a
column's data when its copy list is a *hardcoded* list frozen at some past
column set and the rebuild fires with a newer column populated.
``_migrate_ego_proposals_status_check`` and the ``knowledge_units`` UNIQUE
rebuild were both armed with this latent bug; PR-4 unit C replaced the frozen
copy lists with a runtime column-name intersection (``_intersection_copy``) plus
a drift canary. These tests reproduce the exact loss scenario and lock the fix,
and assert base-vs-rebuild schema parity so a future hand-CREATE re-drift is
caught.

Synthetic in-memory DBs only; wall-clock-independent.
"""

from __future__ import annotations

import importlib
import logging

import aiosqlite

from genesis.db.schema._migrations import (
    _intersection_copy,
    _migrate_add_columns,
    _migrate_ego_proposals_status_check,
    create_all_tables,
)
from genesis.db.schema._tables import TABLES

# Adversarial legacy ego_proposals: the pre-tabled/withdrawn CHECK (so the
# rebuild gate FIRES) while ALSO carrying every post-goal_id column populated —
# the shape a frozen copy list would silently truncate. The ordering in
# _migrate_add_columns prevents this arising naturally, but a reorder/re-arm is
# one edit away; this is the regression lock.
_LEGACY_EGO_OLD_CHECK = """
    CREATE TABLE ego_proposals (
        id TEXT PRIMARY KEY, action_type TEXT NOT NULL,
        action_category TEXT NOT NULL DEFAULT '', content TEXT NOT NULL,
        rationale TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 0.0,
        urgency TEXT NOT NULL DEFAULT 'normal',
        alternatives TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','approved','rejected','expired','executed','failed')),
        user_response TEXT, cycle_id TEXT, batch_id TEXT, created_at TEXT NOT NULL,
        resolved_at TEXT, expires_at TEXT, rank INTEGER, execution_plan TEXT,
        recurring INTEGER DEFAULT 0, memory_basis TEXT DEFAULT '',
        realist_verdict TEXT, realist_reasoning TEXT, ego_source TEXT, goal_id TEXT,
        content_hash TEXT, content_size INTEGER, original_content TEXT,
        expected_outputs TEXT, revision_num INTEGER DEFAULT 1, revalidate_at TEXT,
        last_validated_at TEXT
    )
"""

# Legacy knowledge_units: no UNIQUE(project_type, domain, concept) (so the
# rebuild gate FIRES) while carrying origin_class (added later by 0054) — the
# column the frozen copy list omitted.
_LEGACY_KNOWLEDGE_NO_UNIQUE = """
    CREATE TABLE knowledge_units (
        id TEXT PRIMARY KEY, project_type TEXT NOT NULL, domain TEXT NOT NULL,
        source_doc TEXT NOT NULL, source_platform TEXT, section_title TEXT,
        concept TEXT NOT NULL, body TEXT NOT NULL, relationships TEXT,
        caveats TEXT, tags TEXT, confidence REAL DEFAULT 0.85, source_date TEXT,
        ingested_at TEXT NOT NULL, qdrant_id TEXT, embedding_model TEXT,
        retrieved_count INTEGER NOT NULL DEFAULT 0, source_pipeline TEXT,
        purpose TEXT, ingestion_source TEXT, origin_class TEXT
    )
"""


async def _schema(conn: aiosqlite.Connection, table: str) -> set[tuple]:
    """Full PRAGMA table_info tuples (name, type, notnull, dflt_value)."""
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {(r[1], r[2], r[3], r[4]) for r in await cur.fetchall()}


# ── Landmine-preserve: the data the frozen copy list dropped now survives ──


async def test_ego_rebuild_preserves_newer_column_data():
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(_LEGACY_EGO_OLD_CHECK)
        await conn.execute(
            "INSERT INTO ego_proposals "
            "(id, action_type, content, status, created_at, "
            " content_hash, content_size, expected_outputs, revision_num) "
            "VALUES ('p1','investigate','body','pending','2026-07-01',"
            "        'HASH',42,'EO',3)"
        )
        await conn.commit()
        await _migrate_ego_proposals_status_check(conn)  # gate fires (old CHECK)

        cur = await conn.execute("SELECT sql FROM sqlite_master WHERE name='ego_proposals'")
        ddl = (await cur.fetchone())[0]
        assert "'tabled'" in ddl and "'withdrawn'" in ddl  # CHECK upgraded
        cur = await conn.execute(
            "SELECT content_hash, content_size, expected_outputs, revision_num "
            "FROM ego_proposals WHERE id='p1'"
        )
        # This exact assertion FAILS against the pre-PR-4 frozen copy list.
        assert await cur.fetchone() == ("HASH", 42, "EO", 3)
    finally:
        await conn.close()


async def test_ego_rebuild_failure_leaves_original_intact(monkeypatch):
    import genesis.db.schema._migrations as m

    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(_LEGACY_EGO_OLD_CHECK)
        await conn.execute(
            "INSERT INTO ego_proposals (id, action_type, content, status, created_at) "
            "VALUES ('p1','t','body','pending','2026-07-01')"
        )
        await conn.commit()

        async def boom(*a, **k):
            raise RuntimeError("simulated copy failure")

        monkeypatch.setattr(m, "_intersection_copy", boom)
        # Fail-soft: swallows the error, never raises out of the migration.
        await m._migrate_ego_proposals_status_check(conn)

        cur = await conn.execute("SELECT content FROM ego_proposals WHERE id='p1'")
        assert (await cur.fetchone())[0] == "body"  # original row intact
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE name='ego_proposals_rebuild'"
        )
        assert await cur.fetchone() is None  # temp table cleaned up
    finally:
        await conn.close()


async def test_knowledge_units_rebuild_preserves_newer_column_data():
    conn = await aiosqlite.connect(":memory:")
    try:
        await create_all_tables(conn)
        await conn.execute("DROP TABLE knowledge_units")
        await conn.execute(_LEGACY_KNOWLEDGE_NO_UNIQUE)
        await conn.execute(
            "INSERT INTO knowledge_units "
            "(id, project_type, domain, source_doc, concept, body, ingested_at, origin_class) "
            "VALUES ('k1','p','d','doc','c','b','2026-07-01','OC')"
        )
        await conn.commit()
        await _migrate_add_columns(conn)  # runs the knowledge_units rebuild

        cur = await conn.execute("SELECT sql FROM sqlite_master WHERE name='knowledge_units'")
        assert "UNIQUE(project_type, domain, concept)" in (await cur.fetchone())[0]
        cur = await conn.execute("SELECT origin_class FROM knowledge_units WHERE id='k1'")
        assert (await cur.fetchone())[0] == "OC"  # dropped by pre-PR-4 code
    finally:
        await conn.close()


# ── Column-set parity: legacy-upgrade == fresh, for every touched table ──


async def test_base_vs_rebuild_schema_parity():
    fresh = await aiosqlite.connect(":memory:")
    legacy = await aiosqlite.connect(":memory:")
    try:
        await create_all_tables(fresh)

        await create_all_tables(legacy)
        await legacy.execute("DROP TABLE ego_proposals")
        await legacy.execute(_LEGACY_EGO_OLD_CHECK)
        await legacy.execute("DROP TABLE knowledge_units")
        await legacy.execute(_LEGACY_KNOWLEDGE_NO_UNIQUE)
        await legacy.commit()
        await create_all_tables(legacy)  # rebuilds fire on the swapped tables

        for table in ("ego_proposals", "knowledge_units", "ego_proposal_revisions"):
            assert await _schema(legacy, table) == await _schema(fresh, table), table
    finally:
        await fresh.close()
        await legacy.close()


# ── Numbered ego rebuilds (0007/0012) stay inert on a current DB ──


async def test_numbered_ego_rebuilds_stay_inert_on_current_db():
    conn = await aiosqlite.connect(":memory:")
    try:
        await create_all_tables(conn)
        before = await _schema(conn, "ego_proposals")
        for mod_name in ("0007_ego_proposal_board", "0012_ego_proposals_status_check"):
            mod = importlib.import_module(f"genesis.db.migrations.{mod_name}")
            await mod.up(conn)  # guards early-return; must not shrink the table
        after = await _schema(conn, "ego_proposals")
        assert after == before
        assert len(after) == 30
    finally:
        await conn.close()


# ── Idempotent no-op on already-current DDL ──


async def test_ego_rebuild_idempotent_noop_on_current_ddl():
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(TABLES["ego_proposals"])  # canonical (has tabled/withdrawn)
        await conn.execute(
            "INSERT INTO ego_proposals (id, action_type, content, status, created_at) "
            "VALUES ('p1','t','body','pending','2026-07-01')"
        )
        await conn.commit()
        await _migrate_ego_proposals_status_check(conn)  # gate early-returns

        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE name='ego_proposals_rebuild'"
        )
        assert await cur.fetchone() is None  # no rebuild happened
        cur = await conn.execute("SELECT content FROM ego_proposals WHERE id='p1'")
        assert (await cur.fetchone())[0] == "body"
    finally:
        await conn.close()


# ── Drift canary ──


async def test_intersection_copy_drift_canary_fires_and_copies(caplog):
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute("CREATE TABLE src (a TEXT, b TEXT, extra TEXT)")
        await conn.execute("CREATE TABLE dst (a TEXT, b TEXT)")
        await conn.execute("INSERT INTO src (a, b, extra) VALUES ('1','2','LOST')")
        await conn.commit()
        with caplog.at_level(logging.WARNING):
            await _intersection_copy(conn, src="src", dst="dst")
        assert "extra" in caplog.text and "would be dropped" in caplog.text
        cur = await conn.execute("SELECT a, b FROM dst")
        assert await cur.fetchone() == ("1", "2")
    finally:
        await conn.close()


async def test_intersection_copy_silent_when_target_is_superset(caplog):
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute("CREATE TABLE src (a TEXT, b TEXT)")
        await conn.execute("CREATE TABLE dst (a TEXT, b TEXT, c TEXT)")  # dst ⊇ src
        await conn.execute("INSERT INTO src (a, b) VALUES ('1','2')")
        await conn.commit()
        with caplog.at_level(logging.WARNING):
            await _intersection_copy(conn, src="src", dst="dst")
        assert "would be dropped" not in caplog.text
        cur = await conn.execute("SELECT a, b, c FROM dst")
        assert await cur.fetchone() == ("1", "2", None)  # c takes DEFAULT NULL
    finally:
        await conn.close()


# ── knowledge_units OR IGNORE dedup preserved (first row per key wins) ──


async def test_knowledge_units_rebuild_dedups_first_wins():
    conn = await aiosqlite.connect(":memory:")
    try:
        await create_all_tables(conn)
        await conn.execute("DROP TABLE knowledge_units")
        await conn.execute(_LEGACY_KNOWLEDGE_NO_UNIQUE)
        await conn.execute(
            "INSERT INTO knowledge_units "
            "(id, project_type, domain, source_doc, concept, body, ingested_at) "
            "VALUES ('k1','p','d','doc','c','FIRST','2026-07-01')"
        )
        await conn.execute(
            "INSERT INTO knowledge_units "
            "(id, project_type, domain, source_doc, concept, body, ingested_at) "
            "VALUES ('k2','p','d','doc','c','SECOND','2026-07-02')"
        )
        await conn.commit()
        await _migrate_add_columns(conn)

        cur = await conn.execute(
            "SELECT COUNT(*) FROM knowledge_units "
            "WHERE project_type='p' AND domain='d' AND concept='c'"
        )
        assert (await cur.fetchone())[0] == 1  # deduped by UNIQUE + OR IGNORE
        cur = await conn.execute("SELECT body FROM knowledge_units")
        assert (await cur.fetchone())[0] == "FIRST"  # first row won
    finally:
        await conn.close()
