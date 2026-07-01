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


# --- baseline read must key on completion time, not created_at ---
# Regression: _queue_drop reuses retriable-failed rows via reuse_as_pending,
# which preserves the reused row's OLD created_at. The current baseline is the
# most-recently-COMPLETED row (max processed_at), so ordering the read by
# created_at returned a stale row and dropped the just-evaluated lines →
# re-evaluation on the next scan. (Observed live: a reused row created
# 2026-03-18 but processed today held the full baseline, yet a row created
# today/processed earlier won the created_at sort.)

_FP = "/home/ubuntu/inbox/Genesis.md"


async def _completed(db, *, id, created_at, processed_at, content):
    await inbox_items.create(
        db, id=id, file_path=_FP, content_hash="h",
        status="completed", created_at=created_at,
    )
    await inbox_items.set_response_path(
        db, id, response_path=f"r-{id}", processed_at=processed_at,
        evaluated_content=content,
    )


@pytest.mark.asyncio
async def test_get_evaluated_content_returns_latest_by_processed_at(db):
    # Reused row: OLD created_at, LATEST processed_at, fullest baseline.
    await _completed(db, id="reused", created_at="2026-03-18T00:00:00+00:00",
                     processed_at="2026-06-30T17:39:00+00:00",
                     content="urlA\nurlB")
    # Competing row: NEWER created_at but EARLIER processed_at, less content.
    await _completed(db, id="fresh", created_at="2026-06-30T14:00:00+00:00",
                     processed_at="2026-06-30T16:00:00+00:00",
                     content="urlA")
    base = await inbox_items.get_evaluated_content(db, _FP)
    assert "urlB" in base, (
        "baseline must come from the latest-PROCESSED completed row "
        "(reuse preserves old created_at, so created_at ordering is wrong)"
    )


@pytest.mark.asyncio
async def test_get_last_completed_at_uses_processed_at(db):
    await _completed(db, id="reused", created_at="2026-03-18T00:00:00+00:00",
                     processed_at="2026-06-30T17:39:00+00:00", content="x")
    await _completed(db, id="fresh", created_at="2026-06-30T14:00:00+00:00",
                     processed_at="2026-06-30T16:00:00+00:00", content="x")
    last = await inbox_items.get_last_completed_at(db, _FP)
    assert last == "2026-06-30T17:39:00+00:00"


@pytest.mark.asyncio
async def test_reuse_as_pending_resets_created_at(db):
    # Root-cause fix: re-arming a failed row resets created_at to now, so all
    # created_at-ordered "latest row" / recency / expiry reads stay correct.
    await inbox_items.create(
        db, id="r", file_path=_FP, content_hash="h0", status="failed",
        created_at="2026-03-18T00:00:00+00:00", drop_id="OLD",
        batch_items="old",
    )
    await inbox_items.reuse_as_pending(
        db, "r", drop_id="NEW", batch_items="new", content_hash="h1",
        created_at="2026-06-30T18:00:00+00:00",
    )
    row = await inbox_items.get_by_id(db, "r")
    assert row["status"] == "pending"
    assert row["created_at"] == "2026-06-30T18:00:00+00:00"  # reset, not 03-18
    assert row["drop_id"] == "NEW"
    assert row["batch_items"] == "new"
    assert row["content_hash"] == "h1"


@pytest.mark.asyncio
async def test_get_by_file_path_returns_rearmed_reused_row(db):
    # The created_at-class fix proven end-to-end: get_by_file_path (still
    # created_at-ordered, used for the supersede check) must return the freshly
    # re-armed reused row, not an older completed row — because reuse reset
    # created_at to now.
    await _completed(db, id="done", created_at="2026-06-30T10:00:00+00:00",
                     processed_at="2026-06-30T10:05:00+00:00", content="x")
    await inbox_items.create(
        db, id="reused", file_path=_FP, content_hash="h", status="failed",
        created_at="2026-03-18T00:00:00+00:00",
    )
    await inbox_items.reuse_as_pending(
        db, "reused", drop_id="D", batch_items="new", content_hash="h",
        created_at="2026-06-30T18:00:00+00:00",
    )
    latest = await inbox_items.get_by_file_path(db, _FP)
    assert latest["id"] == "reused", (
        "the re-armed pending row (created_at=now) must be 'latest' so the "
        "supersede check targets it, not a stale older completed row"
    )


@pytest.mark.asyncio
async def test_get_last_completed_at_skips_null_processed_at_meta_rows(db):
    # A real completion, plus a NEWER-created meta completed row (empty/no-delta
    # path) that has NULL processed_at. Must return the real completion time.
    await _completed(db, id="real", created_at="2026-06-30T10:00:00+00:00",
                     processed_at="2026-06-30T10:05:00+00:00", content="x")
    await inbox_items.create(
        db, id="meta", file_path=_FP, content_hash="h", status="completed",
        created_at="2026-06-30T12:00:00+00:00",  # NO processed_at
    )
    last = await inbox_items.get_last_completed_at(db, _FP)
    assert last == "2026-06-30T10:05:00+00:00"


@pytest.mark.asyncio
async def test_get_recent_completed_windows_on_processed_at(db):
    # A reused row keeps an ancient created_at but is COMPLETED now. The digest
    # windows on the last N days; ordering/filtering by created_at would drop it.
    from datetime import UTC, datetime
    now = datetime.now(UTC).isoformat()
    await inbox_items.create(
        db, id="reused", file_path=_FP, content_hash="h", status="completed",
        created_at="2026-03-18T00:00:00+00:00",
    )
    await inbox_items.set_response_path(
        db, "reused", response_path="r-reused", processed_at=now,
        evaluated_content="x",
    )
    recent = await inbox_items.get_recent_completed(db, days=7)
    assert any(r["id"] == "reused" for r in recent), (
        "a reused eval completed today must appear in the digest even though "
        "its created_at is old"
    )
