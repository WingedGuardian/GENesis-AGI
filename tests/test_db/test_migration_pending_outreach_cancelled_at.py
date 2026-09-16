"""Migration 20260909195559 — ``cancelled_at`` onto ``pending_outreach``.

Two properties of ``up()``, mirroring the 0089 pattern for the same table:

1. Idempotency comes from the ``PRAGMA table_info`` guard, not from suppressing
   an error — a re-run is a clean no-op rather than a caught "duplicate column
   name". So a genuine ALTER failure on an existing table still propagates.
2. The ALTER is guarded on ``pending_outreach`` EXISTING, because that table is
   created by ``create_all_tables`` and by ``ensure_table``, never by a
   migration. Exercised in isolation (the migration-runner harness, which has no
   ``create_all_tables``) the table is legitimately absent and ``up()`` skips —
   and must NOT fabricate a stub table, or the real CREATE later finds one
   already there and silently keeps the wrong shape.

This is the path a real upgrade of an existing install takes. ``ensure_table``'s
ALTER covers the subprocess-only boot; this covers the server boot. Both were
written, only one was tested, and mutation verification is what said so.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

_MIG = importlib.import_module("genesis.db.migrations.20260909195559_pending_outreach_cancelled_at")

# The pre-migration shape, trimmed to what the ALTER needs to find.
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


async def _columns(conn, table):
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_up_adds_cancelled_at_idempotently(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await conn.execute(_LEGACY)
        await conn.commit()
        assert "cancelled_at" not in await _columns(conn, "pending_outreach")

        await _MIG.up(conn)
        await conn.commit()
        cols = await _columns(conn, "pending_outreach")
        assert "cancelled_at" in cols

        # Re-run: no-op, and specifically not a second column.
        await _MIG.up(conn)
        await conn.commit()
        assert await _columns(conn, "pending_outreach") == cols
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_up_preserves_existing_rows_as_live(tmp_path):
    """Additive with no backfill: a row that existed before the migration is by
    definition not cancelled, and NULL is exactly what that means. A non-NULL
    default would retroactively mark every queued message cancelled."""
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await conn.execute(_LEGACY)
        await conn.execute(
            "INSERT INTO pending_outreach (id, message, category, created_at) "
            "VALUES ('keep', 'queued before the migration', 'notification', '2026-01-01')"
        )
        await conn.commit()

        await _MIG.up(conn)
        await conn.commit()

        cur = await conn.execute("SELECT cancelled_at FROM pending_outreach WHERE id = 'keep'")
        assert (await cur.fetchone())[0] is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_up_skips_and_fabricates_nothing_when_the_table_is_absent(tmp_path):
    """The runner-in-isolation shape. Must not raise, and must not create a stub —
    a fabricated table would shadow the real CREATE and keep the wrong schema."""
    conn = await aiosqlite.connect(str(tmp_path / "bare.db"))
    try:
        await _MIG.up(conn)  # must not raise "no such table"
        await conn.commit()
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        assert "pending_outreach" not in {r[0] for r in await cur.fetchall()}
    finally:
        await conn.close()
