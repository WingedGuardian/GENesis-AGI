"""Migration 0080: last_extracted_byte on cc_sessions (incremental transcript resume)."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

m0080 = importlib.import_module("genesis.db.migrations.0080_cc_sessions_extracted_byte")


async def _legacy_db() -> aiosqlite.Connection:
    """Pre-0080 cc_sessions shape (has last_extracted_line, no byte offset)."""
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE cc_sessions (
            id TEXT PRIMARY KEY, session_type TEXT NOT NULL, model TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', started_at TEXT NOT NULL,
            last_activity_at TEXT NOT NULL, source_tag TEXT NOT NULL DEFAULT 'foreground',
            last_extracted_at TEXT, last_extracted_line INTEGER DEFAULT 0
        )"""
    )
    await db.commit()
    return db


async def _cols(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")  # noqa: S608
    return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_up_adds_column():
    db = await _legacy_db()
    try:
        await m0080.up(db)
        assert "last_extracted_byte" in await _cols(db, "cc_sessions")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_up_is_idempotent():
    db = await _legacy_db()
    try:
        await m0080.up(db)
        await m0080.up(db)  # duplicate ADD must not raise
        assert "last_extracted_byte" in await _cols(db, "cc_sessions")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_existing_rows_read_null():
    db = await _legacy_db()
    try:
        await db.execute(
            "INSERT INTO cc_sessions (id, session_type, model, started_at, "
            "last_activity_at, last_extracted_line) "
            "VALUES ('s1', 'foreground', 'sonnet', '2026-01-01', '2026-01-01', 500)"
        )
        await m0080.up(db)
        cur = await db.execute("SELECT last_extracted_byte FROM cc_sessions WHERE id='s1'")
        row = await cur.fetchone()
        assert row[0] is None  # NULL = never computed → legacy scan + populate
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_down_drops_column():
    db = await _legacy_db()
    try:
        await m0080.up(db)
        await m0080.down(db)
        assert "last_extracted_byte" not in await _cols(db, "cc_sessions")
    finally:
        await db.close()
