"""Migration 0067 — autonomy_events ledger: idempotence + both build paths.

The migration does NOT commit (the runner owns the transaction); these tests
just read back on the same aiosqlite connection.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M67 = importlib.import_module("genesis.db.migrations.0067_autonomy_events")


async def _table_and_index(db) -> tuple[bool, bool]:
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='autonomy_events'"
    )
    has_table = await cur.fetchone() is not None
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_autonomy_events_cat_time'"
    )
    has_index = await cur.fetchone() is not None
    return has_table, has_index


@pytest.mark.asyncio
async def test_up_is_idempotent(tmp_path):
    db = await aiosqlite.connect(str(tmp_path / "m.db"))
    try:
        await M67.up(db)
        await db.commit()
        await M67.up(db)  # second run must be a no-op, not an error
        await db.commit()
        assert await _table_and_index(db) == (True, True)
        # CHECK constraint enforced
        await db.execute(
            "INSERT INTO autonomy_events (id, category, kind, occurred_at) "
            "VALUES ('e1', 'direct_session', 'success', '2026-07-18T00:00:00+00:00')"
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO autonomy_events (id, category, kind, occurred_at) "
                "VALUES ('e2', 'direct_session', 'bogus', '2026-07-18T00:00:00+00:00')"
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_base_path_has_table(tmp_path):
    """Fresh installs get the table from create_all_tables (both build paths)."""
    from genesis.db.schema import create_all_tables

    db = await aiosqlite.connect(str(tmp_path / "b.db"))
    try:
        await create_all_tables(db)
        assert await _table_and_index(db) == (True, True)
    finally:
        await db.close()
