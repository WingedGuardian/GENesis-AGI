"""Migration 0029 — link_type joins the memory_links primary key (DLI-04 / D15)."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

_OLD_DDL = """
    CREATE TABLE memory_links (
        source_id   TEXT NOT NULL,
        target_id   TEXT NOT NULL,
        link_type   TEXT NOT NULL CHECK (
            link_type IN (
                'supports','contradicts','extends','elaborates',
                'discussed_in','evaluated_for','decided',
                'action_item_for','categorized_as','related_to',
                'succeeded_by','preceded_by'
            )
        ),
        strength    REAL NOT NULL DEFAULT 0.5,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (source_id, target_id)
    )
"""

_MIG = "genesis.db.migrations.0029_memory_links_link_type_pk"


async def _make_fixture_db(path: str, rows: list[tuple] | None = None) -> None:
    """Build memory_links with the OLD (source_id, target_id) PK + seed rows."""
    async with aiosqlite.connect(path) as conn:
        await conn.execute(_OLD_DDL)
        if rows:
            await conn.executemany(
                "INSERT INTO memory_links "
                "(source_id, target_id, link_type, strength, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        await conn.commit()


@pytest.mark.asyncio
async def test_second_type_persists_after_migration(tmp_path) -> None:
    """After migration a 2nd link of a different type for the same pair persists."""
    db_path = tmp_path / "mig.db"
    await _make_fixture_db(
        str(db_path),
        rows=[("a", "b", "supports", 0.5, "2026-01-01")],
    )
    mod = importlib.import_module(_MIG)
    async with aiosqlite.connect(str(db_path)) as conn:
        await mod.up(conn)
        await conn.commit()

        # This INSERT raised IntegrityError under the old PK; now it succeeds.
        await conn.execute(
            "INSERT INTO memory_links "
            "(source_id, target_id, link_type, strength, created_at) "
            "VALUES ('a', 'b', 'contradicts', 0.5, '2026-01-02')"
        )
        await conn.commit()

        cursor = await conn.execute(
            "SELECT link_type FROM memory_links "
            "WHERE source_id='a' AND target_id='b' ORDER BY link_type"
        )
        types = [r[0] for r in await cursor.fetchall()]
        assert types == ["contradicts", "supports"]


@pytest.mark.asyncio
async def test_pre_existing_rows_survive(tmp_path) -> None:
    """All existing rows survive the table rebuild."""
    db_path = tmp_path / "mig.db"
    seed = [
        ("m1", "m2", "supports", 0.5, "2026-01-01"),
        ("m2", "m3", "extends", 0.7, "2026-01-02"),
        ("m3", "m1", "contradicts", 0.5, "2026-01-03"),
    ]
    await _make_fixture_db(str(db_path), rows=seed)
    mod = importlib.import_module(_MIG)
    async with aiosqlite.connect(str(db_path)) as conn:
        await mod.up(conn)
        await conn.commit()
        cursor = await conn.execute("SELECT COUNT(*) FROM memory_links")
        assert (await cursor.fetchone())[0] == 3


@pytest.mark.asyncio
async def test_same_triplet_still_rejected(tmp_path) -> None:
    """The same (source, target, link_type) still violates the new PK."""
    db_path = tmp_path / "mig.db"
    await _make_fixture_db(
        str(db_path),
        rows=[("x", "y", "supports", 0.5, "2026-01-01")],
    )
    mod = importlib.import_module(_MIG)
    async with aiosqlite.connect(str(db_path)) as conn:
        await mod.up(conn)
        await conn.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO memory_links "
                "(source_id, target_id, link_type, strength, created_at) "
                "VALUES ('x', 'y', 'supports', 0.5, '2026-01-02')"
            )
            await conn.commit()


@pytest.mark.asyncio
async def test_indexes_recreated(tmp_path) -> None:
    """The source/target indexes exist after the rebuild."""
    db_path = tmp_path / "mig.db"
    await _make_fixture_db(str(db_path), rows=[("a", "b", "supports", 0.5, "2026-01-01")])
    mod = importlib.import_module(_MIG)
    async with aiosqlite.connect(str(db_path)) as conn:
        await mod.up(conn)
        await conn.commit()
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='memory_links'"
        )
        idx = {r[0] for r in await cursor.fetchall()}
        assert "idx_memory_links_source" in idx
        assert "idx_memory_links_target" in idx


@pytest.mark.asyncio
async def test_idempotent(tmp_path) -> None:
    """Running up() twice is safe (second run detects the new PK and exits)."""
    db_path = tmp_path / "mig.db"
    await _make_fixture_db(
        str(db_path),
        rows=[("a", "b", "supports", 0.5, "2026-01-01")],
    )
    mod = importlib.import_module(_MIG)
    async with aiosqlite.connect(str(db_path)) as conn:
        await mod.up(conn)
        await mod.up(conn)  # must not error or drop data
        await conn.commit()
        cursor = await conn.execute("SELECT COUNT(*) FROM memory_links")
        assert (await cursor.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_missing_table_is_noop(tmp_path) -> None:
    """A fresh DB with no memory_links table: up() is a safe no-op."""
    db_path = tmp_path / "empty.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        mod = importlib.import_module(_MIG)
        await mod.up(conn)  # must not raise
        await conn.commit()
