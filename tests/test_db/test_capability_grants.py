"""Tests for the capability_grants table, migration 0030, and its CRUD (WS-8).

Covers the fresh-install path (create_all_tables / _tables.py), the versioned
migration (up/down/idempotency), CHECK constraints, and CRUD semantics — the
4-state machine persisted, success/correction counters, and the granted→ask
regression below the competence floor.  DARK substrate: no runtime caller yet.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.autonomy.capabilities import InvalidTransition
from genesis.autonomy.types import CellEvent, CellState
from genesis.db.crud import capability_grants as cg

MIGRATION = importlib.import_module("genesis.db.migrations.0030_capability_grants")

_EMAIL = {"domain": "email", "verb": "send", "risk_class": "standard"}
_TS = "2026-06-21T00:00:00"


@pytest.fixture
async def db(tmp_path):
    """Fresh DB with capability_grants created via the real migration up()."""
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await MIGRATION.up(conn)  # up() must not commit — runner owns the txn
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
            "AND name='capability_grants'"
        )
        assert await cur.fetchone() is not None
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_capability_grants_domain'"
        )
        assert await cur.fetchone() is not None

    @pytest.mark.asyncio
    async def test_up_is_idempotent(self, tmp_path):
        path = str(tmp_path / "idem.db")
        async with aiosqlite.connect(path) as conn:
            await MIGRATION.up(conn)
            await MIGRATION.up(conn)  # IF NOT EXISTS → must not raise
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='capability_grants'"
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
                "WHERE type='table' AND name='capability_grants'"
            )
            assert (await cur.fetchone())[0] == 0

    @pytest.mark.asyncio
    async def test_fresh_install_creates_table(self, tmp_path):
        """create_all_tables (the fresh-install / test path) creates it too."""
        from genesis.db.schema import create_all_tables

        path = str(tmp_path / "fresh.db")
        async with aiosqlite.connect(path) as conn:
            await create_all_tables(conn)
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='capability_grants'"
            )
            assert (await cur.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_rejects_bad_state_and_risk_class(self, db):
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO capability_grants (id, domain, verb, risk_class, state) "
                "VALUES ('x', 'email', 'send', 'standard', 'BOGUS')"
            )
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO capability_grants (id, domain, verb, risk_class) "
                "VALUES ('y', 'email', 'send', 'BOGUS')"
            )


# --------------------------------------------------------------------------- #
# CRUD + state machine
# --------------------------------------------------------------------------- #
class TestCrud:
    @pytest.mark.asyncio
    async def test_ensure_creates_not_determined(self, db):
        row = await cg.ensure_cell(db, updated_at=_TS, **_EMAIL)
        assert row["state"] == CellState.NOT_DETERMINED.value
        assert row["id"] == "email:send:standard"
        assert row["successes"] == 0 and row["corrections"] == 0

    @pytest.mark.asyncio
    async def test_ensure_is_idempotent(self, db):
        await cg.ensure_cell(db, updated_at=_TS, **_EMAIL)
        await cg.ensure_cell(db, updated_at=_TS, **_EMAIL)
        rows = await cg.list_all(db)
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_classify_then_approve_grants_and_stamps(self, db):
        s1 = await cg.apply_event(db, event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL)
        assert s1 == CellState.ASK
        s2 = await cg.apply_event(
            db, event=CellEvent.APPROVE, updated_at="2026-06-21T01:00:00", **_EMAIL
        )
        assert s2 == CellState.GRANTED
        row = await cg.get_cell(db, **_EMAIL)
        assert row["state"] == CellState.GRANTED.value
        assert row["granted_at"] == "2026-06-21T01:00:00"

    @pytest.mark.asyncio
    async def test_illegal_event_raises(self, db):
        # APPROVE from NOT_DETERMINED is illegal.
        with pytest.raises(InvalidTransition):
            await cg.apply_event(db, event=CellEvent.APPROVE, updated_at=_TS, **_EMAIL)

    @pytest.mark.asyncio
    async def test_record_success_increments(self, db):
        await cg.record_success(db, updated_at=_TS, **_EMAIL)
        await cg.record_success(db, updated_at=_TS, **_EMAIL)
        row = await cg.get_cell(db, **_EMAIL)
        assert row["successes"] == 2
        assert row["last_used_at"] == _TS

    @pytest.mark.asyncio
    async def test_correction_regresses_granted_cell_below_floor(self, db):
        await cg.apply_event(db, event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL)
        await cg.apply_event(db, event=CellEvent.APPROVE, updated_at=_TS, **_EMAIL)
        # 0 successes, 1 correction → posterior 1/3 < 0.50 → regress to ASK.
        state = await cg.record_correction(db, updated_at=_TS, **_EMAIL)
        assert state == CellState.ASK
        row = await cg.get_cell(db, **_EMAIL)
        assert row["state"] == CellState.ASK.value
        assert row["corrections"] == 1

    @pytest.mark.asyncio
    async def test_correction_keeps_well_supported_grant(self, db):
        await cg.apply_event(db, event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL)
        await cg.apply_event(db, event=CellEvent.APPROVE, updated_at=_TS, **_EMAIL)
        for _ in range(5):
            await cg.record_success(db, updated_at=_TS, **_EMAIL)
        # 5 successes, 1 correction → posterior 6/8 = 0.75 ≥ floor → stays GRANTED.
        state = await cg.record_correction(db, updated_at=_TS, **_EMAIL)
        assert state == CellState.GRANTED

    @pytest.mark.asyncio
    async def test_correction_on_non_granted_is_inert(self, db):
        await cg.apply_event(db, event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL)
        state = await cg.record_correction(db, updated_at=_TS, **_EMAIL)
        assert state == CellState.ASK  # unchanged; only counter moved
        row = await cg.get_cell(db, **_EMAIL)
        assert row["corrections"] == 1

    @pytest.mark.asyncio
    async def test_regrant_restamps_granted_at(self, db):
        # grant → correction-regress → re-approve must refresh granted_at to the
        # most recent grant (the decay clock must not see it as stale-old).
        await cg.apply_event(db, event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL)
        await cg.apply_event(db, event=CellEvent.APPROVE, updated_at=_TS, **_EMAIL)
        assert (await cg.get_cell(db, **_EMAIL))["granted_at"] == _TS
        # 0 successes + 1 correction → regress to ASK.
        assert await cg.record_correction(db, updated_at=_TS, **_EMAIL) == CellState.ASK
        later = "2026-06-22T12:00:00"
        s = await cg.apply_event(db, event=CellEvent.APPROVE, updated_at=later, **_EMAIL)
        assert s == CellState.GRANTED
        assert (await cg.get_cell(db, **_EMAIL))["granted_at"] == later

    @pytest.mark.asyncio
    async def test_correction_atomic_single_state(self, db):
        # The counter increment and regression land together (one UPDATE).
        await cg.apply_event(db, event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL)
        await cg.apply_event(db, event=CellEvent.APPROVE, updated_at=_TS, **_EMAIL)
        state = await cg.record_correction(db, updated_at=_TS, **_EMAIL)
        row = await cg.get_cell(db, **_EMAIL)
        assert state == CellState.ASK
        assert row["state"] == CellState.ASK.value and row["corrections"] == 1

    @pytest.mark.asyncio
    async def test_list_all_orders_by_key(self, db):
        await cg.ensure_cell(db, domain="email", verb="send", risk_class="bulk", updated_at=_TS)
        await cg.ensure_cell(db, updated_at=_TS, **_EMAIL)
        rows = await cg.list_all(db)
        assert [r["risk_class"] for r in rows] == ["bulk", "standard"]
