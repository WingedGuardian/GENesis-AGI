"""Tests for the ego intentions queue — CRUD, context, and processing."""

from __future__ import annotations

import aiosqlite
import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
async def db(tmp_path):
    """In-memory DB with ego_intentions table."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("""
            CREATE TABLE ego_intentions (
                id                TEXT PRIMARY KEY,
                content           TEXT NOT NULL,
                trigger_condition TEXT NOT NULL,
                ego_source        TEXT NOT NULL,
                status            TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'fired', 'expired', 'withdrawn')),
                created_at        TEXT NOT NULL,
                fired_at          TEXT,
                proposal_id       TEXT,
                cycle_count       INTEGER NOT NULL DEFAULT 0,
                max_cycles        INTEGER NOT NULL DEFAULT 20,
                reasoning         TEXT,
                priority          TEXT NOT NULL DEFAULT 'normal'
                    CHECK (priority IN ('low', 'normal', 'high'))
            )
        """)
        await conn.commit()
        yield conn


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------

class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_id(self, db):
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Re-propose LinkedIn outreach",
            trigger_condition="When sprint ends",
            ego_source="user_ego_cycle",
            reasoning="Rejected May 3",
        )
        assert iid is not None
        assert len(iid) == 16

    @pytest.mark.asyncio
    async def test_create_and_list(self, db):
        from genesis.db.crud import ego_intentions

        await ego_intentions.create(
            db,
            content="Investigate Suki culture",
            trigger_condition="When application submitted",
            ego_source="user_ego_cycle",
        )
        items = await ego_intentions.list_active(db, "user_ego_cycle")
        assert len(items) == 1
        assert items[0]["content"] == "Investigate Suki culture"
        assert items[0]["trigger_condition"] == "When application submitted"
        assert items[0]["status"] == "active"
        assert items[0]["cycle_count"] == 0

    @pytest.mark.asyncio
    async def test_cap_enforcement(self, db):
        from genesis.db.crud import ego_intentions

        for i in range(5):
            iid = await ego_intentions.create(
                db,
                content=f"Intention {i}",
                trigger_condition=f"Trigger {i}",
                ego_source="user_ego_cycle",
            )
            assert iid is not None

        # 6th should be rejected
        iid = await ego_intentions.create(
            db,
            content="Intention 5 — over cap",
            trigger_condition="Should not be stored",
            ego_source="user_ego_cycle",
        )
        assert iid is None
        assert await ego_intentions.count_active(db, "user_ego_cycle") == 5

    @pytest.mark.asyncio
    async def test_source_isolation(self, db):
        from genesis.db.crud import ego_intentions

        await ego_intentions.create(
            db,
            content="User intention",
            trigger_condition="User trigger",
            ego_source="user_ego_cycle",
        )
        await ego_intentions.create(
            db,
            content="Genesis intention",
            trigger_condition="Genesis trigger",
            ego_source="genesis_ego_cycle",
        )
        user_items = await ego_intentions.list_active(db, "user_ego_cycle")
        genesis_items = await ego_intentions.list_active(db, "genesis_ego_cycle")
        assert len(user_items) == 1
        assert len(genesis_items) == 1
        assert user_items[0]["content"] == "User intention"
        assert genesis_items[0]["content"] == "Genesis intention"

    @pytest.mark.asyncio
    async def test_cap_per_source(self, db):
        """Cap is per ego_source, not global."""
        from genesis.db.crud import ego_intentions

        for i in range(5):
            await ego_intentions.create(
                db,
                content=f"User {i}",
                trigger_condition=f"T {i}",
                ego_source="user_ego_cycle",
            )
        # Genesis ego should still have room
        iid = await ego_intentions.create(
            db,
            content="Genesis intention",
            trigger_condition="Genesis trigger",
            ego_source="genesis_ego_cycle",
        )
        assert iid is not None


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_fire(self, db):
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Propose X",
            trigger_condition="When Y",
            ego_source="user_ego_cycle",
        )
        ok = await ego_intentions.fire(db, iid, proposal_id="prop_abc")
        assert ok is True

        items = await ego_intentions.list_active(db, "user_ego_cycle")
        assert len(items) == 0  # No longer active

        cursor = await db.execute(
            "SELECT * FROM ego_intentions WHERE id = ?", (iid,),
        )
        row = dict(await cursor.fetchone())
        assert row["status"] == "fired"
        assert row["fired_at"] is not None
        assert row["proposal_id"] == "prop_abc"

    @pytest.mark.asyncio
    async def test_double_fire_fails(self, db):
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Once only",
            trigger_condition="When Z",
            ego_source="user_ego_cycle",
        )
        await ego_intentions.fire(db, iid)
        ok = await ego_intentions.fire(db, iid)
        assert ok is False

    @pytest.mark.asyncio
    async def test_withdraw(self, db):
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Withdraw me",
            trigger_condition="Never",
            ego_source="user_ego_cycle",
        )
        ok = await ego_intentions.withdraw(db, iid)
        assert ok is True

        items = await ego_intentions.list_active(db, "user_ego_cycle")
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_renew(self, db):
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Renew me",
            trigger_condition="Eventually",
            ego_source="user_ego_cycle",
        )
        # Increment a few times
        for _ in range(10):
            await ego_intentions.increment_cycle_count(db, iid)

        # Renew resets to 0
        ok = await ego_intentions.renew(db, iid)
        assert ok is True

        cursor = await db.execute(
            "SELECT cycle_count FROM ego_intentions WHERE id = ?", (iid,),
        )
        row = await cursor.fetchone()
        assert row[0] == 0

    @pytest.mark.asyncio
    async def test_increment_cycle_count(self, db):
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Count me",
            trigger_condition="When counted",
            ego_source="user_ego_cycle",
        )
        new_count = await ego_intentions.increment_cycle_count(db, iid)
        assert new_count == 1
        new_count = await ego_intentions.increment_cycle_count(db, iid)
        assert new_count == 2

    @pytest.mark.asyncio
    async def test_expire_overdue(self, db):
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Short lived",
            trigger_condition="Quick",
            ego_source="user_ego_cycle",
            max_cycles=2,
        )
        await ego_intentions.increment_cycle_count(db, iid)
        await ego_intentions.increment_cycle_count(db, iid)

        expired = await ego_intentions.expire_overdue(db, "user_ego_cycle")
        assert expired == 1

        items = await ego_intentions.list_active(db, "user_ego_cycle")
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_expire_does_not_touch_other_source(self, db):
        from genesis.db.crud import ego_intentions

        await ego_intentions.create(
            db,
            content="User intention",
            trigger_condition="T",
            ego_source="user_ego_cycle",
            max_cycles=1,
        )
        items = await ego_intentions.list_active(db, "user_ego_cycle")
        iid = items[0]["id"]
        await ego_intentions.increment_cycle_count(db, iid)

        # Expire only genesis — user should survive
        expired = await ego_intentions.expire_overdue(db, "genesis_ego_cycle")
        assert expired == 0
        assert await ego_intentions.count_active(db, "user_ego_cycle") == 1


# ---------------------------------------------------------------------------
# Context injection tests
# ---------------------------------------------------------------------------


class TestContextInjection:
    @pytest.mark.asyncio
    async def test_empty_db_shows_placeholder(self, db):
        from genesis.ego.intentions_context import build_intentions_section

        text = await build_intentions_section(db, "user_ego_cycle")
        assert "No active intentions" in text
        assert "MANDATORY REVIEW" not in text

    @pytest.mark.asyncio
    async def test_active_items_rendered(self, db):
        from genesis.db.crud import ego_intentions
        from genesis.ego.intentions_context import build_intentions_section

        iid = await ego_intentions.create(
            db,
            content="Propose LinkedIn outreach",
            trigger_condition="When sprint ends",
            ego_source="user_ego_cycle",
            reasoning="Rejected May 3",
            priority="high",
        )
        text = await build_intentions_section(db, "user_ego_cycle")
        assert "MANDATORY REVIEW" in text
        assert "1 active intention" in text
        assert f"id:{iid}" in text
        assert "Propose LinkedIn outreach" in text
        assert "When sprint ends" in text
        assert "Rejected May 3" in text
        assert "[high]" in text
        assert "cycle 0/20" in text

    @pytest.mark.asyncio
    async def test_remaining_slots(self, db):
        from genesis.db.crud import ego_intentions
        from genesis.ego.intentions_context import build_intentions_section

        for i in range(3):
            await ego_intentions.create(
                db,
                content=f"Intention {i}",
                trigger_condition=f"Trigger {i}",
                ego_source="user_ego_cycle",
            )
        text = await build_intentions_section(db, "user_ego_cycle")
        assert "2 slot(s) available" in text

    @pytest.mark.asyncio
    async def test_source_isolation_in_context(self, db):
        from genesis.db.crud import ego_intentions
        from genesis.ego.intentions_context import build_intentions_section

        await ego_intentions.create(
            db,
            content="User only",
            trigger_condition="User trigger",
            ego_source="user_ego_cycle",
        )
        text = await build_intentions_section(db, "genesis_ego_cycle")
        assert "No active intentions" in text
