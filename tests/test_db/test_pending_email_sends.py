"""Tests for pending_email_sends — table, migration 0031, and CRUD (WS-8 PR-C).

Covers the fresh-install path (create_all_tables), the versioned migration
(up/down/idempotency), the request_id UNIQUE + status CHECK constraints, and
the held→sent / held→rejected transitions including the double-send guard
(a hold can leave 'held' exactly once).
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.crud import pending_email_sends as pes

MIGRATION = importlib.import_module("genesis.db.migrations.0031_pending_email_sends")

_ROW = {
    "id": "p1",
    "request_id": "req-1",
    "validated_recipient": "alice@example.com",
    "category": "outreach",
    "message": "hello there",
    "cell_domain": "email",
    "cell_verb": "send",
    "cell_risk_class": "identity",
    "held_at": "2026-06-21T00:00:00",
}
_TS = "2026-06-21T01:00:00"


@pytest.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await MIGRATION.up(conn)
        await conn.commit()
        yield conn


# --------------------------------------------------------------------------- #
# Schema / migration
# --------------------------------------------------------------------------- #
class TestSchema:
    @pytest.mark.asyncio
    async def test_table_and_index_exist(self, db):
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='pending_email_sends'"
        )
        assert await cur.fetchone() is not None
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_pending_email_sends_status'"
        )
        assert await cur.fetchone() is not None

    @pytest.mark.asyncio
    async def test_up_is_idempotent(self, tmp_path):
        path = str(tmp_path / "idem.db")
        async with aiosqlite.connect(path) as conn:
            await MIGRATION.up(conn)
            await MIGRATION.up(conn)
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='pending_email_sends'"
            )
            assert (await cur.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_down_drops_table(self, tmp_path):
        path = str(tmp_path / "down.db")
        async with aiosqlite.connect(path) as conn:
            await MIGRATION.up(conn)
            await MIGRATION.down(conn)
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='pending_email_sends'"
            )
            assert (await cur.fetchone())[0] == 0

    @pytest.mark.asyncio
    async def test_fresh_install_creates_table(self, tmp_path):
        from genesis.db.schema import create_all_tables

        path = str(tmp_path / "fresh.db")
        async with aiosqlite.connect(path) as conn:
            await create_all_tables(conn)
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='pending_email_sends'"
            )
            assert (await cur.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_rejects_bad_status(self, db):
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO pending_email_sends "
                "(id, request_id, validated_recipient, category, message, "
                " cell_domain, cell_verb, cell_risk_class, held_at, status) "
                "VALUES ('x','r','a@b.c','outreach','m','email','send','identity','t','BOGUS')"
            )

    @pytest.mark.asyncio
    async def test_request_id_unique(self, db):
        await pes.create(db, **_ROW)
        with pytest.raises(aiosqlite.IntegrityError):
            await pes.create(db, **{**_ROW, "id": "p2"})  # same request_id


# --------------------------------------------------------------------------- #
# CRUD + transitions
# --------------------------------------------------------------------------- #
class TestCrud:
    @pytest.mark.asyncio
    async def test_create_and_get(self, db):
        await pes.create(db, **_ROW)
        row = await pes.get_by_id(db, "p1")
        assert row["status"] == "held"
        assert row["validated_recipient"] == "alice@example.com"
        assert row["channel"] == "email"  # default
        assert (await pes.get_by_request(db, "req-1"))["id"] == "p1"

    @pytest.mark.asyncio
    async def test_list_held(self, db):
        await pes.create(db, **_ROW)
        await pes.create(db, **{**_ROW, "id": "p2", "request_id": "req-2"})
        held = await pes.list_held(db)
        assert {r["id"] for r in held} == {"p1", "p2"}

    @pytest.mark.asyncio
    async def test_mark_sent_once(self, db):
        await pes.create(db, **_ROW)
        assert await pes.mark_sent(db, "p1", sent_at=_TS) is True
        # double-send guard: second flip is a no-op.
        assert await pes.mark_sent(db, "p1", sent_at=_TS) is False
        row = await pes.get_by_id(db, "p1")
        assert row["status"] == "sent" and row["sent_at"] == _TS
        assert await pes.list_held(db) == []

    @pytest.mark.asyncio
    async def test_mark_rejected_and_expired(self, db):
        await pes.create(db, **_ROW)
        assert await pes.mark_rejected(db, "p1", rejected_at=_TS) is True
        assert (await pes.get_by_id(db, "p1"))["status"] == "rejected"

        await pes.create(db, **{**_ROW, "id": "p2", "request_id": "req-2"})
        assert await pes.mark_rejected(db, "p2", rejected_at=_TS, expired=True) is True
        assert (await pes.get_by_id(db, "p2"))["status"] == "expired"

    @pytest.mark.asyncio
    async def test_sent_then_reject_is_noop(self, db):
        await pes.create(db, **_ROW)
        await pes.mark_sent(db, "p1", sent_at=_TS)
        # already 'sent' — reject must not override.
        assert await pes.mark_rejected(db, "p1", rejected_at=_TS) is False
        assert (await pes.get_by_id(db, "p1"))["status"] == "sent"
