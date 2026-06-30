"""CRUD tests for inbox drop-batching columns and follow-up dedup."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import follow_ups, inbox_items
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def _mk(db, *, id, drop_id, status="pending", batch_items="x", created_at):
    await inbox_items.create(
        db, id=id, file_path="/inbox/Genesis.md", content_hash="h",
        status=status, created_at=created_at, drop_id=drop_id,
        batch_items=batch_items,
    )


@pytest.mark.asyncio
async def test_create_persists_drop_id_and_batch_items(db):
    await _mk(db, id="a", drop_id="D1", batch_items="line1\nline2",
              created_at="2026-06-30T00:00:00+00:00")
    row = await inbox_items.get_by_id(db, "a")
    assert row["drop_id"] == "D1"
    assert row["batch_items"] == "line1\nline2"


@pytest.mark.asyncio
async def test_get_drop_rows_returns_only_that_drop_ordered(db):
    await _mk(db, id="a", drop_id="D1", created_at="2026-06-30T00:00:01+00:00")
    await _mk(db, id="b", drop_id="D1", created_at="2026-06-30T00:00:02+00:00")
    await _mk(db, id="c", drop_id="D2", created_at="2026-06-30T00:00:03+00:00")
    rows = await inbox_items.get_drop_rows(db, "D1")
    assert [r["id"] for r in rows] == ["a", "b"]


@pytest.mark.asyncio
async def test_get_pending_drop_rows_excludes_completed(db):
    await _mk(db, id="a", drop_id="D1", status="completed",
              created_at="2026-06-30T00:00:01+00:00")
    await _mk(db, id="b", drop_id="D1", status="pending",
              created_at="2026-06-30T00:00:02+00:00")
    await _mk(db, id="c", drop_id="D1", status="processing",
              created_at="2026-06-30T00:00:03+00:00")
    rows = await inbox_items.get_pending_drop_rows(db, "D1")
    assert sorted(r["id"] for r in rows) == ["b", "c"]


@pytest.mark.asyncio
async def test_update_status_for_drop_only_touches_pending_processing(db):
    await _mk(db, id="a", drop_id="D1", status="completed",
              created_at="2026-06-30T00:00:01+00:00")
    await _mk(db, id="b", drop_id="D1", status="pending",
              created_at="2026-06-30T00:00:02+00:00")
    await _mk(db, id="c", drop_id="D1", status="processing",
              created_at="2026-06-30T00:00:03+00:00")
    n = await inbox_items.update_status_for_drop(
        db, "D1", status="failed", error_message="superseded",
        processed_at="2026-06-30T01:00:00+00:00",
    )
    assert n == 2
    assert (await inbox_items.get_by_id(db, "a"))["status"] == "completed"
    assert (await inbox_items.get_by_id(db, "b"))["status"] == "failed"
    assert (await inbox_items.get_by_id(db, "c"))["status"] == "failed"


@pytest.mark.asyncio
async def test_get_awaiting_approval_includes_drop_and_batch_items(db):
    await _mk(db, id="a", drop_id="D1", status="processing",
              batch_items="https://x.com/1", created_at="2026-06-30T00:00:01+00:00")
    await inbox_items.update_status(
        db, "a", status="processing",
        error_message=f"{inbox_items.AWAITING_APPROVAL_PREFIX}req-123",
    )
    rows = await inbox_items.get_awaiting_approval(db)
    assert len(rows) == 1
    assert rows[0]["drop_id"] == "D1"
    assert rows[0]["batch_items"] == "https://x.com/1"


@pytest.mark.asyncio
async def test_follow_up_dedup_key_create_and_exists(db):
    assert await follow_ups.exists_by_dedup_key(db, "k1") is False
    await follow_ups.create(
        db, content="x", source="inbox_evaluation", strategy="ego_judgment",
        dedup_key="k1",
    )
    assert await follow_ups.exists_by_dedup_key(db, "k1") is True
    assert await follow_ups.exists_by_dedup_key(db, "k2") is False
