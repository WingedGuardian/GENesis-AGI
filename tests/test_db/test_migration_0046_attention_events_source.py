"""Migration 0046 — add attention_events.source (device provenance, e.g. omi / edge id).

Verifies the column is added to a pre-existing table, the add is idempotent (fresh DBs
already have it from _tables.py), and a missing table is a safe no-op. Mirrors 0045.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M46 = importlib.import_module("genesis.db.migrations.0046_attention_events_source")

# A minimal PRE-0046 attention_events (no source) — the existing-DB upgrade path.
_OLD_DDL = """
    CREATE TABLE attention_events (
        id TEXT PRIMARY KEY, ts TEXT NOT NULL, session_id TEXT NOT NULL,
        activation TEXT NOT NULL, score REAL NOT NULL, triggers_fired TEXT NOT NULL DEFAULT '[]',
        suppressors TEXT NOT NULL DEFAULT '[]', window_ref TEXT NOT NULL, mode_state TEXT,
        clarity REAL, l15_verdict TEXT, acceptance_signal TEXT, acceptance_note TEXT,
        snapshot_id TEXT, config_version TEXT, created_at TEXT NOT NULL
    )
"""


async def _columns(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute("PRAGMA table_info(attention_events)")
    return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_up_adds_source_column_to_existing_table(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_OLD_DDL)
        assert "source" not in await _columns(db)
        await M46.up(db)
        assert "source" in await _columns(db)


@pytest.mark.asyncio
async def test_up_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_OLD_DDL)
        await M46.up(db)
        await M46.up(db)  # second run must NOT raise "duplicate column"
        assert "source" in await _columns(db)


@pytest.mark.asyncio
async def test_up_noop_when_table_absent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M46.up(db)  # no attention_events table — must be a safe no-op
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='attention_events'"
        )
        assert await cur.fetchone() is None
