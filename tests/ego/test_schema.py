"""Tests for ego DB schema — verify tables and indexes are created correctly."""

import aiosqlite
import pytest

from genesis.db.schema import INDEXES, TABLES


class TestEgoSchema:
    def test_ego_cycles_table_in_schema(self):
        assert "ego_cycles" in TABLES

    def test_ego_proposals_table_in_schema(self):
        assert "ego_proposals" in TABLES

    def test_ego_state_table_in_schema(self):
        assert "ego_state" in TABLES

    @pytest.mark.asyncio
    async def test_ego_cycles_table_creation(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.execute(TABLES["ego_cycles"])
            cursor = await db.execute("PRAGMA table_info(ego_cycles)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "id" in cols
            assert "output_text" in cols
            assert "proposals_json" in cols
            assert "focus_summary" in cols
            assert "model_used" in cols
            assert "cost_usd" in cols
            assert "compacted_into" in cols
            assert "created_at" in cols

    @pytest.mark.asyncio
    async def test_ego_proposals_table_creation(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.execute(TABLES["ego_proposals"])
            cursor = await db.execute("PRAGMA table_info(ego_proposals)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "id" in cols
            assert "action_type" in cols
            assert "action_category" in cols
            assert "content" in cols
            assert "rationale" in cols
            assert "confidence" in cols
            assert "urgency" in cols
            assert "status" in cols
            assert "user_response" in cols
            assert "cycle_id" in cols
            assert "batch_id" in cols
            assert "expires_at" in cols
            # Phase B board columns
            assert "rank" in cols
            assert "execution_plan" in cols
            assert "recurring" in cols

    @pytest.mark.asyncio
    async def test_ego_state_table_creation(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.execute(TABLES["ego_state"])
            cursor = await db.execute("PRAGMA table_info(ego_state)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "key" in cols
            assert "value" in cols
            assert "updated_at" in cols

    @pytest.mark.asyncio
    async def test_ego_proposals_status_constraint(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.execute(TABLES["ego_proposals"])
            # Valid status should work
            await db.execute(
                "INSERT INTO ego_proposals (id, action_type, content, status, created_at) "
                "VALUES ('p1', 'test', 'test', 'pending', '2026-03-28')"
            )
            # Invalid status should fail
            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute(
                    "INSERT INTO ego_proposals (id, action_type, content, status, created_at) "
                    "VALUES ('p2', 'test', 'test', 'INVALID', '2026-03-28')"
                )

    @pytest.mark.asyncio
    async def test_ego_proposals_tabled_status_valid(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.execute(TABLES["ego_proposals"])
            await db.execute(
                "INSERT INTO ego_proposals (id, action_type, content, status, created_at) "
                "VALUES ('p1', 'test', 'test', 'tabled', '2026-04-26')"
            )
            cursor = await db.execute("SELECT status FROM ego_proposals WHERE id='p1'")
            row = await cursor.fetchone()
            assert row[0] == "tabled"

    @pytest.mark.asyncio
    async def test_ego_proposals_withdrawn_status_valid(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.execute(TABLES["ego_proposals"])
            await db.execute(
                "INSERT INTO ego_proposals (id, action_type, content, status, created_at) "
                "VALUES ('p1', 'test', 'test', 'withdrawn', '2026-04-26')"
            )
            cursor = await db.execute("SELECT status FROM ego_proposals WHERE id='p1'")
            row = await cursor.fetchone()
            assert row[0] == "withdrawn"

    @pytest.mark.asyncio
    async def test_ego_proposals_urgency_constraint(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.execute(TABLES["ego_proposals"])
            await db.execute(
                "INSERT INTO ego_proposals (id, action_type, content, urgency, created_at) "
                "VALUES ('p1', 'test', 'test', 'critical', '2026-03-28')"
            )
            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute(
                    "INSERT INTO ego_proposals (id, action_type, content, urgency, created_at) "
                    "VALUES ('p2', 'test', 'test', 'BOGUS', '2026-03-28')"
                )

    def test_ego_indexes_present(self):
        idx_names = [idx for idx in INDEXES if "ego_" in idx]
        assert len(idx_names) >= 9  # 8 original + rank index
        # Check specific critical indexes exist
        idx_text = "\n".join(INDEXES)
        assert "idx_ego_cycles_created" in idx_text
        assert "idx_ego_proposals_status" in idx_text
        assert "idx_ego_proposals_category" in idx_text
        assert "idx_ego_proposals_expires" in idx_text
        assert "idx_ego_proposals_batch" in idx_text
        assert "idx_ego_proposals_rank" in idx_text
