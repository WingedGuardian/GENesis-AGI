"""scripts/inbox_check.py must run numbered migrations before the monitor writes.

``init_db`` cannot widen a CHECK constraint, so a standalone inbox check run
against a DB the server has not yet migrated would write ``status='superseded'``
into the legacy four-value CHECK and fail. The negative control proves the
fixture really is a legacy DB that ``init_db`` alone leaves narrow.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from genesis.db.connection import init_db
from tests.conftest import private_module

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "inbox_check.py"

_LEGACY_INBOX_DDL = """
    CREATE TABLE inbox_items (
        id TEXT PRIMARY KEY, file_path TEXT NOT NULL, content_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (
            status IN ('pending', 'processing', 'completed', 'failed')
        ),
        batch_id TEXT, response_path TEXT, created_at TEXT NOT NULL,
        processed_at TEXT, error_message TEXT
    )
"""

_INSERT = (
    "INSERT INTO inbox_items (id, file_path, content_hash, status, created_at) "
    "VALUES ('x', '/f', 'h', 'superseded', 't')"
)


async def _legacy_db(path: Path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(_LEGACY_INBOX_DDL)
        await db.commit()


@pytest.mark.asyncio
async def test_init_db_alone_leaves_legacy_check_narrow(tmp_path):
    """Negative control: the hazard exists without the migration step."""
    path = tmp_path / "legacy.db"
    await _legacy_db(path)
    db = await init_db(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(_INSERT)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_open_migrated_db_widens_legacy_check(tmp_path):
    mod = private_module("inbox_check_under_test", _SCRIPT)
    path = tmp_path / "legacy.db"
    await _legacy_db(path)
    db = await mod.open_migrated_db(path)
    try:
        await db.execute(_INSERT)
        cur = await db.execute("SELECT status FROM inbox_items WHERE id = 'x'")
        assert (await cur.fetchone())[0] == "superseded"
    finally:
        await db.close()
