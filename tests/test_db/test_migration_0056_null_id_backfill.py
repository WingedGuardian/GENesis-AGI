"""Migration 0056 — backfill NULL-id pending_outreach rows + clear the loop.

SQLite permits NULL in a TEXT PRIMARY KEY, so legacy rows (pre-uuid enqueue)
have id=NULL and can never be marked delivered by id — they re-drain forever.
The migration gives each a deterministic synthetic id and marks it delivered.
Idempotent (WHERE id IS NULL); normal rows are untouched.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M56 = importlib.import_module("genesis.db.migrations.0056_pending_outreach_null_id_backfill")


async def _build(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE pending_outreach (
            id              TEXT PRIMARY KEY,
            message         TEXT NOT NULL,
            category        TEXT NOT NULL,
            channel         TEXT NOT NULL DEFAULT 'telegram',
            urgency         TEXT NOT NULL DEFAULT 'low',
            deliver_after   TEXT,
            created_at      TEXT NOT NULL,
            delivered       INTEGER NOT NULL DEFAULT 0,
            delivered_at    TEXT
        )
        """
    )
    # Two NULL-id undelivered legacy rows + one normal undelivered row.
    await conn.execute(
        "INSERT INTO pending_outreach (message, category, created_at, delivered) "
        "VALUES ('a', 'alert', '2026-04-20T00:00:00+00:00', 0)"
    )
    await conn.execute(
        "INSERT INTO pending_outreach (message, category, created_at, delivered) "
        "VALUES ('b', 'alert', '2026-04-21T00:00:00+00:00', 0)"
    )
    await conn.execute(
        "INSERT INTO pending_outreach (id, message, category, created_at, delivered) "
        "VALUES ('keep', 'c', 'notification', '2026-07-01T00:00:00+00:00', 0)"
    )
    await conn.commit()


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await _build(conn)
        yield conn


@pytest.mark.asyncio
async def test_backfills_null_ids_and_marks_delivered(db):
    await M56.up(db)

    cur = await db.execute("SELECT id, delivered FROM pending_outreach WHERE id IS NULL")
    assert await cur.fetchall() == []  # no NULL ids remain

    cur = await db.execute(
        "SELECT id, delivered FROM pending_outreach WHERE id LIKE 'nullid-backfill-%' ORDER BY id"
    )
    backfilled = await cur.fetchall()
    assert len(backfilled) == 2
    assert all(r["delivered"] == 1 for r in backfilled)

    # The normal row is untouched (still undelivered, id intact).
    cur = await db.execute("SELECT delivered FROM pending_outreach WHERE id = 'keep'")
    assert (await cur.fetchone())["delivered"] == 0


@pytest.mark.asyncio
async def test_idempotent(db):
    await M56.up(db)
    cur = await db.execute(
        "SELECT id, delivered, delivered_at FROM pending_outreach ORDER BY rowid"
    )
    first = [tuple(r) for r in await cur.fetchall()]
    await M56.up(db)  # second run is a no-op (no NULL ids left)
    cur = await db.execute(
        "SELECT id, delivered, delivered_at FROM pending_outreach ORDER BY rowid"
    )
    assert [tuple(r) for r in await cur.fetchall()] == first


@pytest.mark.asyncio
async def test_no_table_is_safe(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "empty.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await M56.up(conn)  # must not raise when the table is absent
