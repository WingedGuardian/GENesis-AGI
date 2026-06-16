"""Migration 0024 — 'conflicts_pending' status in update_history."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest


async def _make_fixture_db(path: str) -> None:
    """Build a DB with update_history as migration 0001 originally creates it."""
    async with aiosqlite.connect(path) as conn:
        await conn.execute("""
            CREATE TABLE update_history (
                id TEXT PRIMARY KEY,
                old_tag TEXT NOT NULL,
                new_tag TEXT NOT NULL,
                old_commit TEXT NOT NULL,
                new_commit TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN ('success', 'failed', 'rolled_back')),
                rollback_tag TEXT,
                failure_reason TEXT,
                degraded_subsystems TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL
            )
        """)
        await conn.commit()


@pytest.mark.asyncio
async def test_conflicts_pending_accepted(tmp_path) -> None:
    """After migration, 'conflicts_pending' is a valid status."""
    db_path = tmp_path / "mig.db"
    await _make_fixture_db(str(db_path))

    mod = importlib.import_module(
        "genesis.db.migrations.0024_update_history_conflicts_status"
    )
    async with aiosqlite.connect(str(db_path)) as conn:
        await mod.up(conn)
        await conn.commit()

        await conn.execute("""
            INSERT INTO update_history
                (id, old_tag, new_tag, old_commit, new_commit, status,
                 started_at, completed_at)
            VALUES
                ('cp-1', 'v1', 'v2', 'abc', 'def', 'conflicts_pending',
                 '2026-06-16T00:00:00Z', '2026-06-16T00:01:00Z')
        """)
        await conn.commit()

        cursor = await conn.execute(
            "SELECT status FROM update_history WHERE id='cp-1'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "conflicts_pending"


@pytest.mark.asyncio
async def test_bogus_status_still_rejected(tmp_path) -> None:
    """Unknown statuses are still rejected after migration."""
    db_path = tmp_path / "mig.db"
    await _make_fixture_db(str(db_path))

    mod = importlib.import_module(
        "genesis.db.migrations.0024_update_history_conflicts_status"
    )
    async with aiosqlite.connect(str(db_path)) as conn:
        await mod.up(conn)
        await conn.commit()

        with pytest.raises(Exception):
            await conn.execute("""
                INSERT INTO update_history
                    (id, old_tag, new_tag, old_commit, new_commit, status,
                     started_at, completed_at)
                VALUES
                    ('bad-1', 'v1', 'v2', 'abc', 'def', 'bogus_status',
                     '2026-06-16T00:00:00Z', '2026-06-16T00:01:00Z')
            """)
            await conn.commit()


@pytest.mark.asyncio
async def test_pre_existing_row_survives(tmp_path) -> None:
    """Existing rows with valid statuses survive the table recreate."""
    db_path = tmp_path / "mig.db"
    await _make_fixture_db(str(db_path))

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("""
            INSERT INTO update_history
                (id, old_tag, new_tag, old_commit, new_commit, status,
                 started_at, completed_at)
            VALUES
                ('pre-1', 'v0', 'v1', 'aaa', 'bbb', 'success',
                 '2026-06-01T00:00:00Z', '2026-06-01T00:05:00Z')
        """)
        await conn.commit()

    mod = importlib.import_module(
        "genesis.db.migrations.0024_update_history_conflicts_status"
    )
    async with aiosqlite.connect(str(db_path)) as conn:
        await mod.up(conn)
        await conn.commit()

        cursor = await conn.execute(
            "SELECT id, status FROM update_history WHERE id='pre-1'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "pre-1"
        assert row[1] == "success"


@pytest.mark.asyncio
async def test_idempotent(tmp_path) -> None:
    """Running up() twice must be safe — second run exits early."""
    db_path = tmp_path / "mig.db"
    await _make_fixture_db(str(db_path))

    mod = importlib.import_module(
        "genesis.db.migrations.0024_update_history_conflicts_status"
    )
    async with aiosqlite.connect(str(db_path)) as conn:
        await mod.up(conn)
        await mod.up(conn)  # must not error or drop the table
        await conn.commit()

        await conn.execute("""
            INSERT INTO update_history
                (id, old_tag, new_tag, old_commit, new_commit, status,
                 started_at, completed_at)
            VALUES
                ('idem-1', 'v1', 'v2', 'aaa', 'bbb', 'conflicts_pending',
                 '2026-06-16T00:00:00Z', '2026-06-16T00:01:00Z')
        """)
        await conn.commit()

        cursor = await conn.execute(
            "SELECT COUNT(*) FROM update_history WHERE id='idem-1'"
        )
        count = (await cursor.fetchone())[0]
        assert count == 1
