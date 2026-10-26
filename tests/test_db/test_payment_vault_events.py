"""payment_vault_events: migration/fresh-install parity and the CRUD contract."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.crud import payment_vault_events as audit
from genesis.db.schema._tables import INDEXES, TABLES

MIG = importlib.import_module("genesis.db.migrations.20261011060631_payment_vault_events")


async def _schema(db) -> dict:
    cursor = await db.execute(
        "SELECT name, sql FROM sqlite_master WHERE name LIKE '%payment_vault_events%'"
    )
    return dict(await cursor.fetchall())


async def test_fresh_install_ddl_and_migration_are_the_same_schema(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "fresh.db")) as fresh:
        await fresh.execute(TABLES["payment_vault_events"])
        for ddl in INDEXES:
            if "payment_vault_events" in ddl:
                await fresh.execute(ddl)
        fresh_schema = await _schema(fresh)
    async with aiosqlite.connect(str(tmp_path / "migrated.db")) as migrated:
        await MIG.up(migrated)
        await MIG.up(migrated)  # idempotent
        migrated_schema = await _schema(migrated)
    assert fresh_schema == migrated_schema
    assert set(fresh_schema) == {"payment_vault_events", "idx_payment_vault_events_created"}


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "g.db")) as conn:
        await MIG.up(conn)
        yield conn


async def test_record_and_list_newest_first(db):
    await audit.record_event(db, "service_start", {"pid": 1})
    await audit.record_event(db, "unlock", {"peer": {"pid": 2, "comm": "python"}})
    await audit.record_event(db, "lock")
    rows = await audit.list_recent(db)
    assert [r["event"] for r in rows] == ["lock", "unlock", "service_start"]
    assert rows[1]["detail"] == {"peer": {"pid": 2, "comm": "python"}}
    assert rows[0]["detail"] is None and rows[0]["created_at"]
    assert len(await audit.list_recent(db, 1)) == 1


async def test_the_key_file_events_are_recorded_and_the_old_ones_still_are(db):
    """key_loaded / key_load_refused / key_dropped (vault_service.ensure_key) are
    key events; the passphrase-era names stay valid so earlier rows still read."""
    new = ("key_loaded", "key_load_refused", "key_dropped")
    old = ("unlock", "unlock_refused", "lock", "idle_relock", "stale_identity_relock")
    for event in (*old, *new):
        await audit.record_event(db, event, {"reason": "r"})
    rows = await audit.list_recent(db, events=audit.KEY_EVENTS)
    assert [r["event"] for r in rows] == list(reversed((*old, *new)))
    assert set(new) <= audit.KEY_EVENTS


async def test_unknown_event_is_refused(db):
    with pytest.raises(ValueError, match="unknown payment vault event"):
        await audit.record_event(db, "card_released")
    assert await audit.list_recent(db) == []
