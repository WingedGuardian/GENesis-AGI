"""Tests for procedural_memory quarantine CRUD and schema migration."""

from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.db.crud import procedural
from genesis.db.schema import create_all_tables, seed_data


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


async def _insert_procedure(db, id: str, *, quarantined: int = 0, deprecated: int = 0,
                             success_count: int = 5, failure_count: int = 1):
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO procedural_memory "
        "(id, task_type, principle, steps, tools_used, context_tags, "
        "success_count, failure_count, confidence, deprecated, quarantined, created_at) "
        "VALUES (?, 'test', 'principle', '[]', '[]', '[]', ?, ?, 0.8, ?, ?, ?)",
        (id, success_count, failure_count, deprecated, quarantined, now),
    )
    await db.commit()


class TestQuarantineColumn:
    @pytest.mark.asyncio
    async def test_column_exists(self, db):
        """The quarantined column should exist after create_all_tables."""
        cursor = await db.execute("PRAGMA table_info(procedural_memory)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "quarantined" in columns

    @pytest.mark.asyncio
    async def test_default_value_is_zero(self, db):
        now = datetime.now(UTC).isoformat()
        await procedural.create(
            db, id="p1", task_type="test", principle="x",
            steps=["a"], tools_used=[], context_tags=[], created_at=now,
        )
        row = await procedural.get_by_id(db, "p1")
        assert row["quarantined"] == 0

    @pytest.mark.asyncio
    async def test_migration_idempotent(self, db):
        """Running create_all_tables again shouldn't fail."""
        await create_all_tables(db)
        cursor = await db.execute("PRAGMA table_info(procedural_memory)")
        columns = [row[1] for row in await cursor.fetchall()]
        assert columns.count("quarantined") == 1


class TestQuarantineCrud:
    @pytest.mark.asyncio
    async def test_quarantine(self, db):
        await _insert_procedure(db, "p1")
        result = await procedural.quarantine(db, "p1")
        assert result
        row = await procedural.get_by_id(db, "p1")
        assert row["quarantined"] == 1

    @pytest.mark.asyncio
    async def test_unquarantine(self, db):
        await _insert_procedure(db, "p1", quarantined=1)
        result = await procedural.unquarantine(db, "p1")
        assert result
        row = await procedural.get_by_id(db, "p1")
        assert row["quarantined"] == 0

    @pytest.mark.asyncio
    async def test_quarantine_nonexistent(self, db):
        result = await procedural.quarantine(db, "nonexistent")
        assert not result

    @pytest.mark.asyncio
    async def test_list_active_excludes_quarantined(self, db):
        await _insert_procedure(db, "p1")
        await _insert_procedure(db, "p2", quarantined=1)
        await _insert_procedure(db, "p3", deprecated=1)
        active = await procedural.list_active(db)
        assert len(active) == 1
        assert active[0]["id"] == "p1"

    @pytest.mark.asyncio
    async def test_list_quarantined(self, db):
        await _insert_procedure(db, "p1")
        await _insert_procedure(db, "p2", quarantined=1)
        await _insert_procedure(db, "p3", quarantined=1)
        quarantined = await procedural.list_quarantined(db)
        assert len(quarantined) == 2
