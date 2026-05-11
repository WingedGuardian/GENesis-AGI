"""Migration 0016 — source_subsystem column + reflection backfill."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest


async def _make_fixture_db(path: str) -> None:
    """Build a DB with the bare tables migration 0016 touches."""
    async with aiosqlite.connect(path) as conn:
        await conn.execute("""
            CREATE TABLE memory_metadata (
                memory_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                collection TEXT NOT NULL DEFAULT 'episodic_memory',
                confidence REAL,
                embedding_status TEXT NOT NULL DEFAULT 'embedded',
                memory_class TEXT DEFAULT 'fact',
                wing TEXT,
                room TEXT,
                valid_at TEXT,
                invalid_at TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE pending_embeddings (
                id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                content TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                collection TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE VIRTUAL TABLE memory_fts USING fts5(
                memory_id, content, source_type, tags, collection
            )
        """)
        await conn.commit()


async def _seed_rows(conn: aiosqlite.Connection) -> None:
    """Seed memory_metadata + memory_fts with diverse subsystem-shape rows."""
    rows = [
        # (memory_id, wing, room, tags-in-fts)
        ("user-1", "infrastructure", "database", "ingest_summary class:fact"),
        ("refl-obs-1", "learning", "reflection",
         "reflection_observation obs:abc class:fact"),
        ("refl-sum-1", "learning", "reflection",
         "reflection_summary obs:def class:fact"),
        ("ego-1", "autonomy", "ego_corrections",
         "ego_correction action_category class:rule"),
        ("triage-1", "general", "signals",
         "tier:HIGH profile:recon class:fact"),
    ]
    for mid, wing, room, tags in rows:
        await conn.execute(
            "INSERT INTO memory_metadata (memory_id, created_at, wing, room) "
            "VALUES (?, '2026-05-10T00:00:00Z', ?, ?)",
            (mid, wing, room),
        )
        await conn.execute(
            "INSERT INTO memory_fts (memory_id, content, source_type, tags, collection) "
            "VALUES (?, ?, 'memory', ?, 'episodic_memory')",
            (mid, f"content for {mid}", tags),
        )
    await conn.commit()


@pytest.mark.asyncio
async def test_migration_adds_column_and_index(tmp_path) -> None:
    db_path = tmp_path / "mig.db"
    await _make_fixture_db(str(db_path))

    mod = importlib.import_module(
        "genesis.db.migrations.0016_memory_subsystem_tag"
    )
    async with aiosqlite.connect(str(db_path)) as conn:
        await mod.up(conn)
        await conn.commit()

        # memory_metadata.source_subsystem
        cursor = await conn.execute("PRAGMA table_info(memory_metadata)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "source_subsystem" in cols

        # pending_embeddings.source_subsystem
        cursor = await conn.execute("PRAGMA table_info(pending_embeddings)")
        pe_cols = {row[1] for row in await cursor.fetchall()}
        assert "source_subsystem" in pe_cols

        # index exists
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_memory_meta_subsystem'"
        )
        assert await cursor.fetchone() is not None


@pytest.mark.asyncio
async def test_reflection_backfill_only(tmp_path) -> None:
    """Backfill tags reflection rows only; ego/triage stay NULL (forward-only)."""
    db_path = tmp_path / "mig.db"
    await _make_fixture_db(str(db_path))
    async with aiosqlite.connect(str(db_path)) as conn:
        await _seed_rows(conn)

    mod = importlib.import_module(
        "genesis.db.migrations.0016_memory_subsystem_tag"
    )
    async with aiosqlite.connect(str(db_path)) as conn:
        await mod.up(conn)
        await conn.commit()

        cursor = await conn.execute(
            "SELECT memory_id, source_subsystem FROM memory_metadata "
            "ORDER BY memory_id"
        )
        result = dict(await cursor.fetchall())

    assert result["refl-obs-1"] == "reflection"
    assert result["refl-sum-1"] == "reflection"
    # Ego and triage are NOT backfilled — only forward-tagged
    assert result["ego-1"] is None
    assert result["triage-1"] is None
    # User row untouched
    assert result["user-1"] is None


@pytest.mark.asyncio
async def test_idempotent(tmp_path) -> None:
    """Running up() twice must be safe."""
    db_path = tmp_path / "mig.db"
    await _make_fixture_db(str(db_path))
    async with aiosqlite.connect(str(db_path)) as conn:
        await _seed_rows(conn)

    mod = importlib.import_module(
        "genesis.db.migrations.0016_memory_subsystem_tag"
    )
    async with aiosqlite.connect(str(db_path)) as conn:
        await mod.up(conn)
        await mod.up(conn)  # second run — no errors, no double-tagging
        await conn.commit()

        cursor = await conn.execute(
            "SELECT COUNT(*) FROM memory_metadata "
            "WHERE source_subsystem = 'reflection'"
        )
        count = (await cursor.fetchone())[0]
        assert count == 2  # exactly the two reflection rows


@pytest.mark.asyncio
async def test_down_drops_column_and_index(tmp_path) -> None:
    db_path = tmp_path / "mig.db"
    await _make_fixture_db(str(db_path))

    mod = importlib.import_module(
        "genesis.db.migrations.0016_memory_subsystem_tag"
    )
    async with aiosqlite.connect(str(db_path)) as conn:
        await mod.up(conn)
        await conn.commit()
        await mod.down(conn)
        await conn.commit()

        cursor = await conn.execute("PRAGMA table_info(memory_metadata)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "source_subsystem" not in cols

        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_memory_meta_subsystem'"
        )
        assert await cursor.fetchone() is None
