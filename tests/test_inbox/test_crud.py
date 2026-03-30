"""Tests for inbox_items CRUD operations."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import inbox_items
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_create_and_get(db):
    item_id = await inbox_items.create(
        db, id="item-1", file_path="/inbox/test.md",
        content_hash="abc123", created_at="2026-03-10T00:00:00",
    )
    assert item_id == "item-1"
    row = await inbox_items.get_by_id(db, "item-1")
    assert row is not None
    assert row["file_path"] == "/inbox/test.md"
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_get_by_file_path(db):
    await inbox_items.create(
        db, id="item-1", file_path="/inbox/test.md",
        content_hash="abc", created_at="2026-03-10T00:00:00",
    )
    row = await inbox_items.get_by_file_path(db, "/inbox/test.md")
    assert row is not None
    assert row["id"] == "item-1"


@pytest.mark.asyncio
async def test_get_by_file_path_returns_latest(db):
    await inbox_items.create(
        db, id="item-1", file_path="/inbox/test.md",
        content_hash="v1", created_at="2026-03-10T00:00:00",
    )
    await inbox_items.create(
        db, id="item-2", file_path="/inbox/test.md",
        content_hash="v2", created_at="2026-03-10T01:00:00",
    )
    row = await inbox_items.get_by_file_path(db, "/inbox/test.md")
    assert row["id"] == "item-2"


@pytest.mark.asyncio
async def test_get_all_known(db):
    await inbox_items.create(
        db, id="i1", file_path="/inbox/a.md",
        content_hash="h1", created_at="2026-03-10T00:00:00",
    )
    await inbox_items.create(
        db, id="i2", file_path="/inbox/b.md",
        content_hash="h2", created_at="2026-03-10T00:00:00",
    )
    known = await inbox_items.get_all_known(db)
    assert known == {"/inbox/a.md": "h1", "/inbox/b.md": "h2"}


@pytest.mark.asyncio
async def test_get_all_known_excludes_failed(db):
    await inbox_items.create(
        db, id="i1", file_path="/inbox/a.md",
        content_hash="h1", created_at="2026-03-10T00:00:00",
    )
    await inbox_items.update_status(db, "i1", status="failed", error_message="bad")
    known = await inbox_items.get_all_known(db)
    assert known == {}


@pytest.mark.asyncio
async def test_update_status(db):
    await inbox_items.create(
        db, id="i1", file_path="/inbox/a.md",
        content_hash="h1", created_at="2026-03-10T00:00:00",
    )
    ok = await inbox_items.update_status(db, "i1", status="processing")
    assert ok is True
    row = await inbox_items.get_by_id(db, "i1")
    assert row["status"] == "processing"


@pytest.mark.asyncio
async def test_set_batch(db):
    await inbox_items.create(
        db, id="i1", file_path="/inbox/a.md",
        content_hash="h1", created_at="2026-03-10T00:00:00",
    )
    ok = await inbox_items.set_batch(db, "i1", batch_id="batch-1")
    assert ok is True
    row = await inbox_items.get_by_id(db, "i1")
    assert row["batch_id"] == "batch-1"


@pytest.mark.asyncio
async def test_set_response_path(db):
    await inbox_items.create(
        db, id="i1", file_path="/inbox/a.md",
        content_hash="h1", created_at="2026-03-10T00:00:00",
    )
    ok = await inbox_items.set_response_path(
        db, "i1", response_path="/inbox/_genesis/resp.md",
        processed_at="2026-03-10T01:00:00",
    )
    assert ok is True
    row = await inbox_items.get_by_id(db, "i1")
    assert row["status"] == "completed"
    assert row["response_path"] == "/inbox/_genesis/resp.md"


@pytest.mark.asyncio
async def test_query_pending(db):
    await inbox_items.create(
        db, id="i1", file_path="/inbox/a.md",
        content_hash="h1", created_at="2026-03-10T00:00:00",
    )
    await inbox_items.create(
        db, id="i2", file_path="/inbox/b.md",
        content_hash="h2", created_at="2026-03-10T01:00:00",
    )
    await inbox_items.update_status(db, "i2", status="completed")
    pending = await inbox_items.query_pending(db)
    assert len(pending) == 1
    assert pending[0]["id"] == "i1"


@pytest.mark.asyncio
async def test_query_by_batch(db):
    await inbox_items.create(
        db, id="i1", file_path="/inbox/a.md",
        content_hash="h1", created_at="2026-03-10T00:00:00",
        batch_id="batch-1",
    )
    await inbox_items.create(
        db, id="i2", file_path="/inbox/b.md",
        content_hash="h2", created_at="2026-03-10T00:00:00",
        batch_id="batch-1",
    )
    await inbox_items.create(
        db, id="i3", file_path="/inbox/c.md",
        content_hash="h3", created_at="2026-03-10T00:00:00",
        batch_id="batch-2",
    )
    batch1 = await inbox_items.query_by_batch(db, "batch-1")
    assert len(batch1) == 2
