"""Migration 0063 — add ``user_goals.origin`` (goal provenance).

Verifies the column is added to a pre-existing table with the 'user' default
backfilled onto existing rows (every pre-migration goal predates ego
autonomy, so it is a user directive), the CHECK rejects unknown origins, the
add is idempotent (fresh DBs already have it from _tables.py), and a missing
table is a safe no-op. Mirrors 0048.
"""

from __future__ import annotations

import importlib
import sqlite3

import aiosqlite
import pytest

M63 = importlib.import_module("genesis.db.migrations.0063_user_goals_origin")

# A minimal PRE-0063 user_goals (no origin) — the existing-DB upgrade path.
_OLD_DDL = """
    CREATE TABLE user_goals (
        id TEXT PRIMARY KEY, title TEXT NOT NULL,
        category TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'medium',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""


async def _columns(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute("PRAGMA table_info(user_goals)")
    return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_up_adds_origin_column_to_existing_table(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_OLD_DDL)
        assert "origin" not in await _columns(db)
        await M63.up(db)
        assert "origin" in await _columns(db)


@pytest.mark.asyncio
async def test_existing_rows_backfilled_as_user(tmp_path):
    """Every pre-migration goal predates genesis-ego autonomy — treat it as a
    user directive (never autonomously touchable)."""
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_OLD_DDL)
        await db.execute(
            "INSERT INTO user_goals (id, title, category) VALUES ('g-old', 't', 'project')"
        )
        await M63.up(db)
        cur = await db.execute("SELECT origin FROM user_goals WHERE id = 'g-old'")
        row = await cur.fetchone()
        assert row[0] == "user"


@pytest.mark.asyncio
async def test_check_rejects_unknown_origin(tmp_path):
    """origin is the security boundary for additive ego autonomy — the CHECK
    must reject anything outside {'user','genesis_ego'}."""
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_OLD_DDL)
        await M63.up(db)
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "INSERT INTO user_goals (id, title, category, origin) "
                "VALUES ('g-bad', 't', 'project', 'bogus')"
            )


@pytest.mark.asyncio
async def test_up_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_OLD_DDL)
        await M63.up(db)
        await M63.up(db)  # second run must NOT raise "duplicate column"
        assert "origin" in await _columns(db)


@pytest.mark.asyncio
async def test_up_noop_when_table_absent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M63.up(db)  # no user_goals table — must be a safe no-op
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_goals'"
        )
        assert await cur.fetchone() is None


@pytest.mark.asyncio
async def test_fresh_canonical_table_has_origin(tmp_path):
    """Fresh installs get origin from the canonical CREATE TABLE."""
    from genesis.db.schema import TABLES

    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(TABLES["user_goals"])
        cols = await _columns(db)
        assert "origin" in cols
        # Default must be 'user' (fail-safe: an unstamped create is a user goal).
        await db.execute(
            "INSERT INTO user_goals (id, title, category) VALUES ('g-f', 't', 'project')"
        )
        cur = await db.execute("SELECT origin FROM user_goals WHERE id = 'g-f'")
        assert (await cur.fetchone())[0] == "user"
