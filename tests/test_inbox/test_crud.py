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
async def test_get_all_known_newest_created_at_wins(db):
    """When a file has multiple non-failed rows, get_all_known must return the
    NEWEST-created_at row's hash, not an arbitrary (insertion-order) one.

    Regression (detection storm): a reused row resets created_at to now but
    keeps its old (low) rowid, so rowid/insertion order != recency. Without a
    deterministic ORDER BY, a stale row's hash could win -> the file's current
    hash never matches 'known' -> phantom-modified every scan.
    """
    # Insert the NEWER-created_at row FIRST (lower rowid) to prove ORDER BY
    # (recency), not insertion order, decides the winner.
    await inbox_items.create(
        db, id="new", file_path="/inbox/a.md", content_hash="NEWHASH",
        status="completed", created_at="2026-03-10T20:00:00+00:00",
    )
    await inbox_items.create(
        db, id="old", file_path="/inbox/a.md", content_hash="OLDHASH",
        status="completed", created_at="2026-03-10T10:00:00+00:00",
    )
    known = await inbox_items.get_all_known(db)
    assert known["/inbox/a.md"] == "NEWHASH"


@pytest.mark.asyncio
async def test_get_all_known_excludes_retriable_failed_keeps_completed(db):
    """A retriable-failed row (retry_count < max) is excluded from 'known' so
    the file stays re-detectable for retry — even when a completed sibling of
    the same file exists (which supplies the known hash)."""
    await inbox_items.create(
        db, id="done", file_path="/inbox/a.md", content_hash="H",
        status="completed", created_at="2026-03-10T10:00:00+00:00",
    )
    await inbox_items.create(
        db, id="failed", file_path="/inbox/a.md", content_hash="H2",
        status="pending", created_at="2026-03-10T20:00:00+00:00",
    )
    await inbox_items.update_status(db, "failed", status="failed", error_message="rate limit")
    known = await inbox_items.get_all_known(db)
    # Completed sibling supplies the hash; the retriable-failed newer row is excluded.
    assert known["/inbox/a.md"] == "H"


@pytest.mark.asyncio
async def test_get_all_known_reused_row_wins_over_older_completed(db):
    """The exact live storm trigger: reuse_as_pending re-arms a failed row —
    resetting created_at to now but KEEPING its old (low) rowid — and re-points
    it at the current drop's hash. get_all_known must return that re-armed row's
    (current) hash, not an older completed sibling's, or the file's on-disk hash
    never matches 'known' and it re-detects as modified every scan.
    """
    # The to-be-reused row is inserted FIRST -> low rowid. It will be re-armed
    # to created_at=now (newest) while keeping that low rowid, so rowid order
    # would pick the WRONG (older completed) row without the created_at ORDER BY.
    await inbox_items.create(
        db, id="reused", file_path="/inbox/a.md", content_hash="STALE",
        status="pending", created_at="2026-01-01T00:00:00+00:00",
    )
    await inbox_items.update_status(db, "reused", status="failed", error_message="rate limit")
    # Older completed sibling inserted SECOND -> higher rowid, older created_at.
    await inbox_items.create(
        db, id="done", file_path="/inbox/a.md", content_hash="OLDHASH",
        status="completed", created_at="2026-03-10T10:00:00+00:00",
    )
    # Re-arm the failed row for the current drop: created_at=now (newest).
    await inbox_items.reuse_as_pending(
        db, "reused", drop_id="D", batch_items="new-url",
        content_hash="CURRENT", created_at="2026-03-10T20:00:00+00:00",
    )
    known = await inbox_items.get_all_known(db)
    assert known["/inbox/a.md"] == "CURRENT"


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
