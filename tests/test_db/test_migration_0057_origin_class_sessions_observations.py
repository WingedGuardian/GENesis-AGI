"""Migration 0057: origin_class on cc_sessions + observations (WS-3 B4 PR-2)."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

m0057 = importlib.import_module(
    "genesis.db.migrations.0057_origin_class_sessions_observations"
)


async def _legacy_db() -> aiosqlite.Connection:
    """Pre-0057 shapes of both tables (no origin_class)."""
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE cc_sessions (
            id TEXT PRIMARY KEY, session_type TEXT NOT NULL, model TEXT NOT NULL,
            effort TEXT NOT NULL DEFAULT 'medium', status TEXT NOT NULL DEFAULT 'active',
            started_at TEXT NOT NULL, last_activity_at TEXT NOT NULL,
            source_tag TEXT NOT NULL DEFAULT 'foreground', metadata TEXT
        )"""
    )
    await db.execute(
        """CREATE TABLE observations (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, type TEXT NOT NULL,
            content TEXT NOT NULL, priority TEXT NOT NULL, created_at TEXT NOT NULL
        )"""
    )
    await db.commit()
    return db


async def _cols(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")  # noqa: S608
    return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_up_adds_both_columns():
    db = await _legacy_db()
    try:
        await m0057.up(db)
        assert "origin_class" in await _cols(db, "cc_sessions")
        assert "origin_class" in await _cols(db, "observations")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_up_is_idempotent():
    db = await _legacy_db()
    try:
        await m0057.up(db)
        await m0057.up(db)  # second run must not raise (duplicate column)
        assert "origin_class" in await _cols(db, "cc_sessions")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_existing_rows_read_null():
    db = await _legacy_db()
    try:
        await db.execute(
            "INSERT INTO cc_sessions (id, session_type, model, started_at, last_activity_at)"
            " VALUES ('s1', 'foreground', 'sonnet', '2026-01-01', '2026-01-01')"
        )
        await m0057.up(db)
        cur = await db.execute("SELECT origin_class FROM cc_sessions WHERE id='s1'")
        row = await cur.fetchone()
        assert row[0] is None  # historical NULL = unknown, read first_party in shadow
    finally:
        await db.close()
