"""Migration 0086 — labeled_surplus threading onto pending_outreach.

Locks two properties of ``up()`` after the ``contextlib.suppress`` around the
ALTER was removed (marketing cold-send PR1, round-3 item E):

1. Idempotency comes from the ``_has_column`` PRAGMA guard, NOT the suppress — a
   re-run is a clean no-op.
2. An ALTER error now PROPAGATES (no silent swallow) — so a transient SQLITE_LOCKED
   reaches the runner's retry-on-lock loop, and a genuine failure fails the
   migration loudly instead of recording 0086 applied WITHOUT the column (which
   would break every subsequent pending_outreach enqueue).
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

_MIG = importlib.import_module("genesis.db.migrations.0086_marketing_prospects")


async def _columns(conn, table):
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_up_threads_labeled_surplus_idempotently(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        # Existing-install shape: pending_outreach exists WITHOUT labeled_surplus.
        await conn.execute("CREATE TABLE pending_outreach (id TEXT)")

        await _MIG.up(conn)
        cols = await _columns(conn, "pending_outreach")
        assert cols.count("labeled_surplus") == 1  # added once
        # up() also creates the marketing_prospects table.
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='marketing_prospects'"
        )
        assert await cur.fetchone() is not None

        # Re-run must be a clean no-op (the _has_column guard, not a suppress).
        await _MIG.up(conn)
        cols2 = await _columns(conn, "pending_outreach")
        assert cols2.count("labeled_surplus") == 1  # still exactly one
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_up_propagates_alter_error_no_silent_suppress(tmp_path):
    # If pending_outreach is absent, the ALTER cannot succeed. With the suppress
    # removed, up() must RAISE rather than silently record 0086 as applied without
    # the column. (This is the RED lock for item E: the pre-fix code swallowed this.)
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        with pytest.raises(aiosqlite.OperationalError):
            await _MIG.up(conn)
    finally:
        await conn.close()
