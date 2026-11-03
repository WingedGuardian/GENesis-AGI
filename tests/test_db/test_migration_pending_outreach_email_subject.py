"""Migration 20261028063000: ``email_subject`` onto ``pending_outreach``.

Same contract as the ``cancelled_at`` migration for this table: idempotent by a
``PRAGMA table_info`` guard, no backfill (an existing row has no subject of its
own, and NULL makes the drain fall back to the message start, as before), and a
no-op when the table is absent (``create_all_tables`` and ``ensure_table`` own
its creation). ``ensure_table`` adds the column on the subprocess-only boot.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.crud import pending_outreach

_MIG = importlib.import_module(
    "genesis.db.migrations.20261028063000_pending_outreach_email_subject"
)

_LEGACY = """
    CREATE TABLE pending_outreach (
        id           TEXT PRIMARY KEY,
        message      TEXT NOT NULL,
        category     TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        delivered    INTEGER NOT NULL DEFAULT 0,
        delivered_at TEXT
    )
"""


async def _columns(conn):
    cur = await conn.execute("PRAGMA table_info(pending_outreach)")
    return [r[1] for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_up_adds_email_subject_idempotently_and_keeps_rows(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await conn.execute(_LEGACY)
        await conn.execute(
            "INSERT INTO pending_outreach (id, message, category, created_at) "
            "VALUES ('keep', 'queued before', 'notification', '2026-01-01')"
        )
        await conn.commit()
        await _MIG.up(conn)
        await conn.commit()
        cols = await _columns(conn)
        assert "email_subject" in cols
        cur = await conn.execute("SELECT email_subject FROM pending_outreach WHERE id = 'keep'")
        assert (await cur.fetchone())[0] is None
        await _MIG.up(conn)
        await conn.commit()
        assert await _columns(conn) == cols
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_up_skips_when_the_table_is_absent(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "bare.db"))
    try:
        await _MIG.up(conn)
        await conn.commit()
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        assert "pending_outreach" not in {r[0] for r in await cur.fetchall()}
    finally:
        await conn.close()


_BEFORE_SUBJECT = """
    CREATE TABLE pending_outreach (
        id                  TEXT PRIMARY KEY,
        message             TEXT NOT NULL,
        category            TEXT NOT NULL,
        channel             TEXT NOT NULL DEFAULT 'telegram',
        urgency             TEXT NOT NULL DEFAULT 'low',
        deliver_after       TEXT,
        created_at          TEXT NOT NULL,
        delivered           INTEGER NOT NULL DEFAULT 0,
        delivered_at        TEXT,
        thread_id           TEXT,
        validated_recipient TEXT,
        labeled_surplus     INTEGER NOT NULL DEFAULT 0,
        cancelled_at        TEXT
    )
"""


@pytest.mark.asyncio
async def test_ensure_table_adds_email_subject_to_a_legacy_table(tmp_path):
    # The shape just before this column: the subprocess-only boot upgrades it.
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await conn.execute(_BEFORE_SUBJECT)
        await conn.commit()
        await pending_outreach.ensure_table(conn)
        assert "email_subject" in await _columns(conn)
        await pending_outreach.enqueue(
            conn, message="m", category="notification", channel="email", email_subject="S"
        )
        cur = await conn.execute("SELECT email_subject FROM pending_outreach")
        assert (await cur.fetchone())[0] == "S"
    finally:
        await conn.close()
