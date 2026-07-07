"""Migration 0048 — add task_states.source (dispatch provenance).

Verifies the column is added to a pre-existing table with the 'user' default
backfilled onto existing rows, the add is idempotent (fresh DBs already have
it from _tables.py), and a missing table is a safe no-op. Mirrors 0046.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M48 = importlib.import_module("genesis.db.migrations.0048_task_states_source")

# A minimal PRE-0048 task_states (no source) — the existing-DB upgrade path.
_OLD_DDL = """
    CREATE TABLE task_states (
        task_id TEXT PRIMARY KEY, description TEXT NOT NULL,
        current_phase TEXT NOT NULL DEFAULT 'planning', decisions TEXT,
        blockers TEXT, outputs TEXT, session_id TEXT, intake_token TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""


async def _columns(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute("PRAGMA table_info(task_states)")
    return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_up_adds_source_column_to_existing_table(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_OLD_DDL)
        assert "source" not in await _columns(db)
        await M48.up(db)
        assert "source" in await _columns(db)


@pytest.mark.asyncio
async def test_existing_rows_backfilled_as_user(tmp_path):
    """Every pre-migration row was a /task user submission — the only path
    that existed — so the DEFAULT backfill must label them 'user'."""
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_OLD_DDL)
        await db.execute(
            "INSERT INTO task_states (task_id, description) VALUES ('t-old', 'd')"
        )
        await M48.up(db)
        cur = await db.execute(
            "SELECT source FROM task_states WHERE task_id = 't-old'"
        )
        row = await cur.fetchone()
        assert row[0] == "user"


@pytest.mark.asyncio
async def test_up_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_OLD_DDL)
        await M48.up(db)
        await M48.up(db)  # second run must NOT raise "duplicate column"
        assert "source" in await _columns(db)


@pytest.mark.asyncio
async def test_up_noop_when_table_absent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M48.up(db)  # no task_states table — must be a safe no-op
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_states'"
        )
        assert await cur.fetchone() is None
