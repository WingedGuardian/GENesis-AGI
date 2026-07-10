"""Migration 0053 — origin_class column + deterministic backfill.

Simulates a PRE-0053 database: legacy CREATE TABLEs for knowledge_units and
memory_metadata copied from the current DDL in ``db/schema/_tables.py`` minus
the ``origin_class`` column. Seeds one row per backfill archetype, runs
``up()``, and asserts per-row classes, zero NULLs, and idempotency (a second
``up()`` leaves every row identical).

The migration does NOT commit (the runner owns the transaction); these tests
just read back on the same aiosqlite connection.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M53 = importlib.import_module("genesis.db.migrations.0054_origin_class")

# Current knowledge_units DDL from _tables.py WITHOUT origin_class.
_LEGACY_KNOWLEDGE_UNITS = """
    CREATE TABLE knowledge_units (
        id               TEXT PRIMARY KEY,
        project_type     TEXT NOT NULL,
        domain           TEXT NOT NULL,
        source_doc       TEXT NOT NULL,
        source_platform  TEXT,
        section_title    TEXT,
        concept          TEXT NOT NULL,
        body             TEXT NOT NULL,
        relationships    TEXT,
        caveats          TEXT,
        tags             TEXT,
        confidence       REAL DEFAULT 0.85,
        source_date      TEXT,
        ingested_at      TEXT NOT NULL,
        qdrant_id        TEXT,
        embedding_model  TEXT,
        retrieved_count  INTEGER NOT NULL DEFAULT 0,
        source_pipeline  TEXT,
        purpose          TEXT,
        ingestion_source TEXT,
        UNIQUE(project_type, domain, concept)
    )
"""

# Current memory_metadata DDL from _tables.py WITHOUT origin_class.
_LEGACY_MEMORY_METADATA = """
    CREATE TABLE memory_metadata (
        memory_id        TEXT PRIMARY KEY,
        created_at       TEXT NOT NULL,
        collection       TEXT NOT NULL DEFAULT 'episodic_memory',
        confidence       REAL,
        embedding_status TEXT NOT NULL DEFAULT 'embedded',
        memory_class     TEXT DEFAULT 'fact',
        wing             TEXT,
        room             TEXT,
        valid_at         TEXT,
        invalid_at       TEXT,
        source_subsystem TEXT,
        deprecated       INTEGER NOT NULL DEFAULT 0,
        dream_cycle_run_id TEXT
    )
"""

_NOW = "2026-07-10T00:00:00+00:00"


async def _seed_ku(
    db: aiosqlite.Connection, *, id: str, source_pipeline: str | None,
    qdrant_id: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO knowledge_units "
        "(id, project_type, domain, source_doc, concept, body, ingested_at, "
        " qdrant_id, source_pipeline) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (id, "proj", "dom", "doc", f"concept-{id}", "body", _NOW,
         qdrant_id, source_pipeline),
    )


async def _seed_mm(
    db: aiosqlite.Connection, *, memory_id: str, collection: str,
    source_subsystem: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO memory_metadata "
        "(memory_id, created_at, collection, source_subsystem) "
        "VALUES (?, ?, ?, ?)",
        (memory_id, _NOW, collection, source_subsystem),
    )


async def _seed_archetypes(db: aiosqlite.Connection) -> None:
    # (a) plain episodic row
    await _seed_mm(db, memory_id="mm-episodic", collection="episodic_memory")
    # (b) subsystem write (episodic, source_subsystem set)
    await _seed_mm(
        db, memory_id="mm-subsystem", collection="episodic_memory",
        source_subsystem="triage",
    )
    # (c) KB row joined via qdrant_id to a ku with a Genesis-authored pipeline
    await _seed_ku(
        db, id="ku-surplus", source_pipeline="surplus", qdrant_id="mm-kb-surplus",
    )
    await _seed_mm(db, memory_id="mm-kb-surplus", collection="knowledge_base")
    # (d) KB row joined to a ku with a world-derived pipeline
    await _seed_ku(
        db, id="ku-curated", source_pipeline="curated", qdrant_id="mm-kb-curated",
    )
    await _seed_mm(db, memory_id="mm-kb-curated", collection="knowledge_base")
    # (e) KB row with NO matching ku
    await _seed_mm(db, memory_id="mm-kb-orphan", collection="knowledge_base")
    # (f) legacy ku with NULL source_pipeline (and no joined mm)
    await _seed_ku(db, id="ku-null-pipeline", source_pipeline=None)


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    rows = await db.execute_fetchall(f"PRAGMA table_info({table})")
    return {r[1] for r in rows}


async def _mm_classes(db: aiosqlite.Connection) -> dict[str, str | None]:
    rows = await db.execute_fetchall(
        "SELECT memory_id, origin_class FROM memory_metadata"
    )
    return dict(rows)


async def _ku_classes(db: aiosqlite.Connection) -> dict[str, str | None]:
    rows = await db.execute_fetchall(
        "SELECT id, origin_class FROM knowledge_units"
    )
    return dict(rows)


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute(_LEGACY_KNOWLEDGE_UNITS)
    await conn.execute(_LEGACY_MEMORY_METADATA)
    await _seed_archetypes(conn)
    yield conn
    await conn.close()


async def test_up_adds_columns(db):
    assert "origin_class" not in await _columns(db, "knowledge_units")
    assert "origin_class" not in await _columns(db, "memory_metadata")
    await M53.up(db)
    assert "origin_class" in await _columns(db, "knowledge_units")
    assert "origin_class" in await _columns(db, "memory_metadata")


async def test_backfill_per_archetype(db):
    await M53.up(db)

    mm = await _mm_classes(db)
    assert mm["mm-episodic"] == "first_party"          # (a)
    assert mm["mm-subsystem"] == "first_party"         # (b)
    assert mm["mm-kb-surplus"] == "first_party"        # (c) via ku join
    assert mm["mm-kb-curated"] == "external_untrusted"  # (d) via ku join
    assert mm["mm-kb-orphan"] == "external_untrusted"  # (e) no ku → conservative

    ku = await _ku_classes(db)
    assert ku["ku-surplus"] == "first_party"           # (c)
    assert ku["ku-curated"] == "external_untrusted"    # (d)
    assert ku["ku-null-pipeline"] == "external_untrusted"  # (f)


async def test_backfill_leaves_no_nulls(db):
    await M53.up(db)
    for table in ("knowledge_units", "memory_metadata"):
        rows = await db.execute_fetchall(
            f"SELECT COUNT(*) FROM {table} WHERE origin_class IS NULL"  # noqa: S608 - fixed test table names
        )
        assert rows[0][0] == 0, f"{table} has NULL origin_class rows"


async def test_up_is_idempotent(db):
    await M53.up(db)
    first_mm = await _mm_classes(db)
    first_ku = await _ku_classes(db)

    await M53.up(db)  # must not raise, must not change any row
    assert await _mm_classes(db) == first_mm
    assert await _ku_classes(db) == first_ku


async def test_up_tolerates_missing_tables():
    """PRAGMA-guarded: a DB without either table is a no-op, not an error."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await M53.up(conn)  # neither table exists
        conn2_ddl_only = conn  # same connection, add just memory_metadata
        await conn2_ddl_only.execute(_LEGACY_MEMORY_METADATA)
        await _seed_mm(
            conn2_ddl_only, memory_id="mm-kb", collection="knowledge_base",
        )
        await M53.up(conn2_ddl_only)  # ku table absent → KB rows external
        rows = await conn2_ddl_only.execute_fetchall(
            "SELECT origin_class FROM memory_metadata WHERE memory_id='mm-kb'"
        )
        assert rows[0][0] == "external_untrusted"
    finally:
        await conn.close()
