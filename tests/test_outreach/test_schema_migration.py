"""Tests for Phase 8 schema migration (delivery_id column)."""

import aiosqlite
import pytest

from genesis.db.crud import outreach as outreach_crud
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_delivery_id_column_exists(db):
    cursor = await db.execute("PRAGMA table_info(outreach_history)")
    columns = {row[1] for row in await cursor.fetchall()}
    assert "delivery_id" in columns


@pytest.mark.asyncio
async def test_create_with_delivery_id(db):
    await outreach_crud.create(
        db,
        id="test-1",
        signal_type="surplus_insight",
        topic="Test",
        category="surplus",
        salience_score=0.8,
        channel="telegram",
        message_content="Hello",
        created_at="2026-03-12T00:00:00Z",
        delivery_id="tg-msg-123",
    )
    row = await outreach_crud.get_by_id(db, "test-1")
    assert row["delivery_id"] == "tg-msg-123"


@pytest.mark.asyncio
async def test_find_by_delivery_id(db):
    await outreach_crud.create(
        db,
        id="test-2",
        signal_type="morning_report",
        topic="Morning",
        category="digest",
        salience_score=0.0,
        channel="telegram",
        message_content="Good morning",
        created_at="2026-03-12T07:00:00Z",
        delivery_id="tg-msg-456",
    )
    row = await outreach_crud.find_by_delivery_id(db, "tg-msg-456")
    assert row is not None
    assert row["id"] == "test-2"
