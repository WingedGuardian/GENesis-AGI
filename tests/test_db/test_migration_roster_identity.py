"""Migration 20260905194140: identity columns on session_heartbeats.

The roster's one missing edge — which OS process a cc_session_id belongs to —
gets stored where the in-session hooks can write it. All columns nullable, no
backfill: pre-migration rows honestly render identity-unknown.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

mig = importlib.import_module(
    "genesis.db.migrations.20260905194140_roster_identity_columns"
)

_EXPECTED = {"pid", "pid_started_at", "cwd", "git_branch", "slot"}


async def _legacy_db() -> aiosqlite.Connection:
    """The pre-migration session_heartbeats shape."""
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE session_heartbeats (
            cc_session_id   TEXT PRIMARY KEY,
            source_tag      TEXT NOT NULL DEFAULT 'foreground',
            model           TEXT,
            topic           TEXT,
            user_summary    TEXT,
            genesis_summary TEXT,
            updated_at      TEXT NOT NULL
        )"""
    )
    await db.commit()
    return db


async def _cols(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute("PRAGMA table_info(session_heartbeats)")
    return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_up_adds_all_identity_columns():
    db = await _legacy_db()
    try:
        await mig.up(db)
        assert await _cols(db) >= _EXPECTED
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_up_is_idempotent():
    db = await _legacy_db()
    try:
        await mig.up(db)
        await mig.up(db)  # second run must be a no-op, not an error
        assert await _cols(db) >= _EXPECTED
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_up_on_missing_table_is_noop():
    """Fresh DB: create_all_tables carries the columns; up() must not create
    a table the base path owns."""
    db = await aiosqlite.connect(":memory:")
    try:
        await mig.up(db)  # no session_heartbeats at all — silent no-op
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE name='session_heartbeats'"
        )
        assert await cur.fetchone() is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_down_strips_them():
    db = await _legacy_db()
    try:
        await mig.up(db)
        await mig.down(db)
        assert not (_EXPECTED & await _cols(db))
    finally:
        await db.close()
