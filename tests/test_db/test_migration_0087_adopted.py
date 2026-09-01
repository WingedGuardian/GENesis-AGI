"""Migration 0087 — pending_issue_posts.adopted (create-vs-adopt provenance).

Locks that ``up()`` adds the column on an existing-install table and is a clean
no-op on re-run (idempotency comes from the ``_has_column`` guard).
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

_MIG = importlib.import_module("genesis.db.migrations.0087_pending_issue_posts_adopted")


async def _columns(conn, table):
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_up_adds_adopted_idempotently(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        # Existing-install shape: pending_issue_posts without the adopted column.
        await conn.execute("CREATE TABLE pending_issue_posts (id TEXT)")

        await _MIG.up(conn)
        assert (await _columns(conn, "pending_issue_posts")).count("adopted") == 1

        # Re-run must be a clean no-op (the _has_column guard, not a suppress).
        await _MIG.up(conn)
        assert (await _columns(conn, "pending_issue_posts")).count("adopted") == 1
    finally:
        await conn.close()
