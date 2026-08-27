"""Migration 0086 — labeled_surplus threading onto pending_outreach.

Locks two properties of ``up()`` (marketing cold-send PR1, round-3 item E — the
``contextlib.suppress`` around the ALTER was removed and replaced with a table-
existence guard):

1. Idempotency comes from the ``_has_column`` PRAGMA guard — a re-run is a clean
   no-op (does NOT re-add the column).
2. The ALTER is guarded on ``pending_outreach`` EXISTING. That table is created by
   ``create_all_tables`` (NOT by any migration), so when the runner is exercised in
   isolation — no ``create_all_tables`` first, e.g. the migration-runner test harness
   — the table is legitimately absent and ``up()`` SKIPS the ALTER (no "no such table"
   failure) rather than crashing the whole migration run. This is NOT a blanket error
   suppress: the guard only sidesteps the not-applicable case; a real ALTER failure on
   an existing table (a transient SQLITE_LOCKED, a genuine schema error) still
   propagates to the runner.
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
async def test_up_skips_labeled_surplus_when_table_absent(tmp_path):
    # Standalone-runner shape (no create_all_tables): pending_outreach does not exist.
    # up() must complete WITHOUT raising — it still creates marketing_prospects, and
    # simply skips threading labeled_surplus (there is no table to thread; create_all_
    # tables will create pending_outreach WITH the column). This is the regression the
    # migration-runner test harness caught when the ALTER was left unguarded.
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await _MIG.up(conn)  # must NOT raise
        # marketing_prospects was created; no stray pending_outreach was fabricated.
        tables = [
            r[0]
            for r in await (
                await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        ]
        assert "marketing_prospects" in tables
        assert "pending_outreach" not in tables
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_up_propagates_real_alter_error_on_existing_table(tmp_path, monkeypatch):
    # The non-suppression property item E demanded: when the table EXISTS and the
    # ALTER genuinely fails (here a simulated SQLITE_LOCKED), up() must PROPAGATE the
    # error — NOT swallow it (a swallow would record 0086 applied without the column).
    # Guards against a future regression that re-wraps the ALTER in a suppress.
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await conn.execute("CREATE TABLE pending_outreach (id TEXT)")  # exists, no column
        real_execute = conn.execute

        async def boom(sql, *a, **k):
            if isinstance(sql, str) and sql.lstrip().upper().startswith(
                "ALTER TABLE PENDING_OUTREACH"
            ):
                raise aiosqlite.OperationalError("database is locked")
            return await real_execute(sql, *a, **k)

        monkeypatch.setattr(conn, "execute", boom)
        with pytest.raises(aiosqlite.OperationalError):
            await _MIG.up(conn)  # guard passes → ALTER runs → error must NOT be swallowed
    finally:
        await conn.close()
