"""Tests for cognitive_file_modifications — migration 0027 + its CRUD.

Covers the versioned migration (up/down/idempotency), the fresh-install path
(create_all_tables / _tables.py), and CRUD semantics (record/get/recent/
counts_by_target/mark_rolled_back/prune_keep_per_target).
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.crud import cognitive_file_modifications as cfm

# Migration module name starts with a digit — import via importlib.
MIGRATION = importlib.import_module(
    "genesis.db.migrations.0027_cognitive_file_modifications"
)


@pytest.fixture
async def db(tmp_path):
    """Fresh DB with the table created via the real migration up()."""
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        await MIGRATION.up(conn)  # up() must not commit — runner owns the txn
        await conn.commit()
        yield conn


# --------------------------------------------------------------------------- #
# Schema / migration
# --------------------------------------------------------------------------- #
class TestSchema:
    @pytest.mark.asyncio
    async def test_table_and_indexes_exist(self, db):
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='cognitive_file_modifications'"
        )
        assert await cur.fetchone() is not None

        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name LIKE 'idx_cog_file_mods_%'"
        )
        idx = {r[0] for r in await cur.fetchall()}
        assert {
            "idx_cog_file_mods_target",
            "idx_cog_file_mods_actor",
            "idx_cog_file_mods_created",
            "idx_cog_file_mods_status",
        } <= idx

    @pytest.mark.asyncio
    async def test_up_is_idempotent(self, tmp_path):
        path = str(tmp_path / "idem.db")
        async with aiosqlite.connect(path) as conn:
            await MIGRATION.up(conn)
            await MIGRATION.up(conn)  # IF NOT EXISTS → must not raise
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='cognitive_file_modifications'"
            )
            assert (await cur.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_down_drops_table(self, tmp_path):
        path = str(tmp_path / "down.db")
        async with aiosqlite.connect(path) as conn:
            await MIGRATION.up(conn)
            await conn.commit()
            await MIGRATION.down(conn)
            await conn.commit()
            cur = await conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='cognitive_file_modifications'"
            )
            assert await cur.fetchone() is None

    @pytest.mark.asyncio
    async def test_fresh_install_path_creates_table(self, tmp_path):
        from genesis.db.schema import create_all_tables

        path = str(tmp_path / "fresh.db")
        async with aiosqlite.connect(path) as conn:
            await create_all_tables(conn)
            await conn.commit()
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='cognitive_file_modifications'"
            )
            assert await cur.fetchone() is not None


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
class TestRecord:
    @pytest.mark.asyncio
    async def test_record_returns_id_and_persists(self, db):
        mid = await cfm.record(
            db, actor="skill_evolution", target_path="/skills/x/SKILL.md",
            prior_content="old", applied_content="new",
            change_summary="tweak", metadata={"skill": "x"},
        )
        assert isinstance(mid, str) and len(mid) == 16
        row = await cfm.get(db, mid)
        assert row["actor"] == "skill_evolution"
        assert row["prior_content"] == "old"
        assert row["applied_content"] == "new"
        assert row["status"] == "applied"
        assert row["rolled_back_at"] is None
        assert row["metadata"] == {"skill": "x"}  # parsed back to dict

    @pytest.mark.asyncio
    async def test_record_allows_null_prior(self, db):
        mid = await cfm.record(
            db, actor="a", target_path="/p", applied_content="new",
        )
        row = await cfm.get(db, mid)
        assert row["prior_content"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"actor": "", "target_path": "/p", "applied_content": "x"},
            {"actor": "a", "target_path": "", "applied_content": "x"},
            {"actor": "a", "target_path": "/p", "applied_content": None},
        ],
    )
    async def test_record_validates(self, db, kwargs):
        with pytest.raises(ValueError):
            await cfm.record(db, **kwargs)

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, db):
        assert await cfm.get(db, "nope") is None


class TestQueries:
    @pytest.mark.asyncio
    async def test_recent_newest_first_and_actor_filter(self, db):
        await cfm.record(db, actor="a", target_path="/1", applied_content="x",
                         created_at="2026-01-01T00:00:01")
        await cfm.record(db, actor="b", target_path="/2", applied_content="y",
                         created_at="2026-01-01T00:00:02")
        await cfm.record(db, actor="a", target_path="/3", applied_content="z",
                         created_at="2026-01-01T00:00:03")

        rows = await cfm.recent(db, limit=10)
        assert [r["target_path"] for r in rows] == ["/3", "/2", "/1"]

        only_a = await cfm.recent(db, limit=10, actor="a")
        assert [r["target_path"] for r in only_a] == ["/3", "/1"]

    @pytest.mark.asyncio
    async def test_counts_by_target(self, db):
        await cfm.record(db, actor="a", target_path="/p", applied_content="1",
                         created_at="2026-01-01T00:00:01")
        await cfm.record(db, actor="a", target_path="/p", applied_content="2",
                         created_at="2026-01-01T00:00:02")
        await cfm.record(db, actor="a", target_path="/q", applied_content="3",
                         created_at="2026-01-01T00:00:03")
        counts = await cfm.counts_by_target(db)
        by_path = {c["target_path"]: c for c in counts}
        assert by_path["/p"]["n"] == 2
        assert by_path["/q"]["n"] == 1
        assert by_path["/p"]["rolled_back"] == 0


class TestRollbackState:
    @pytest.mark.asyncio
    async def test_mark_rolled_back(self, db):
        mid = await cfm.record(db, actor="a", target_path="/p", applied_content="x")
        assert await cfm.mark_rolled_back(db, mid) is True
        row = await cfm.get(db, mid)
        assert row["status"] == "rolled_back"
        assert row["rolled_back_at"] is not None
        # missing id → False
        assert await cfm.mark_rolled_back(db, "nope") is False


class TestPrune:
    @pytest.mark.asyncio
    async def test_prune_keeps_most_recent_n(self, db):
        for i in range(5):
            await cfm.record(
                db, actor="a", target_path="/p", applied_content=str(i),
                created_at=f"2026-01-01T00:00:0{i}",
            )
        # Another target must be untouched by the per-target prune.
        await cfm.record(db, actor="a", target_path="/other", applied_content="o")

        deleted = await cfm.prune_keep_per_target(db, "/p", keep=2)
        assert deleted == 3

        rows = await cfm.recent(db, limit=10, actor="a")
        kept_p = [r["applied_content"] for r in rows if r["target_path"] == "/p"]
        assert kept_p == ["4", "3"]  # the two most recent
        assert any(r["target_path"] == "/other" for r in rows)
