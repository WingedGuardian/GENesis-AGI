"""Migration 0038 — add ``thread_id`` + ``validated_recipient`` to pending_outreach.

A subprocess (``pipeline=None``) ``outreach_send`` falls back to enqueueing into
``pending_outreach``; before this the fallback dropped both the thread id and the
resolved recipient, so a queued email follow-up arrived recipient-less and the
drain defaulted it to the agent's own address (a self-send spam loop). These two
nullable columns let the drain reconstruct a properly-routed request.

The test builds the *pre-migration* schema explicitly (no new columns) so it
exercises the real ``ALTER TABLE … ADD COLUMN`` regardless of current DDL.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M38 = importlib.import_module(
    "genesis.db.migrations.0038_pending_outreach_thread_recipient"
)


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _build_pre_state(conn: aiosqlite.Connection) -> None:
    """pending_outreach as it existed *before* 0038 — no thread_id /
    validated_recipient columns."""
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
    await conn.commit()


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await _build_pre_state(conn)
        yield conn


@pytest.mark.asyncio
async def test_adds_both_columns(db):
    await M38.up(db)
    cols = await _columns(db, "pending_outreach")
    assert "thread_id" in cols
    assert "validated_recipient" in cols


@pytest.mark.asyncio
async def test_preserves_existing_rows(db):
    await db.execute(
        "INSERT INTO pending_outreach (id, message, category, created_at) "
        "VALUES ('r1', 'hello', 'notification', '2026-06-24T00:00:00+00:00')"
    )
    await db.commit()

    await M38.up(db)

    cur = await db.execute(
        "SELECT message, thread_id, validated_recipient FROM pending_outreach "
        "WHERE id='r1'"
    )
    row = await cur.fetchone()
    assert row["message"] == "hello"
    assert row["thread_id"] is None          # new column, no backfill
    assert row["validated_recipient"] is None


@pytest.mark.asyncio
async def test_idempotent_on_rerun(db):
    await M38.up(db)
    await M38.up(db)  # must not raise "duplicate column name"
    cols = await _columns(db, "pending_outreach")
    assert {"thread_id", "validated_recipient"} <= cols


@pytest.mark.asyncio
async def test_skips_when_base_table_absent(tmp_path):
    """The runner applies migrations against a bare DB; 0038 must skip cleanly
    rather than fail on a missing table."""
    async with aiosqlite.connect(str(tmp_path / "bare.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("CREATE TABLE schema_migrations (version TEXT)")
        await conn.commit()
        await M38.up(conn)  # must not raise (no such table: pending_outreach)
