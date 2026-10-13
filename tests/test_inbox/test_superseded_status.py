"""inbox_items ``status='superseded'`` — writers and readers.

A parked row replaced by a newer snapshot is not a failure. These pin that the
supersession writer uses the new status (keeping the audit reason and the retry
budget) and that every status-keyed reader treats it as terminal-and-inert:
invisible to the known-hash scan, never a retry candidate, never live.
"""

from __future__ import annotations

import sqlite3

import aiosqlite
import pytest

from genesis.db.crud import inbox_items
from genesis.db.schema import create_all_tables

_FILE = "/inbox/links.md"
_T0 = "2026-09-01T00:00:00+00:00"
_T1 = "2026-09-02T00:00:00+00:00"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def _parked(db, rid, *, reqid="req-1", retry_count=0, created_at=_T0):
    await inbox_items.create(
        db,
        id=rid,
        file_path=_FILE,
        content_hash=f"hash-{rid}",
        status="processing",
        created_at=created_at,
        error_message=f"{inbox_items.AWAITING_APPROVAL_PREFIX}{reqid}",
        retry_count=retry_count,
    )


@pytest.mark.asyncio
async def test_supersede_parked_rows_writes_superseded(db):
    await _parked(db, "p1", retry_count=2)
    n = await inbox_items.supersede_parked_rows(db, _FILE, processed_at=_T1)
    assert n == 1
    row = await inbox_items.get_by_id(db, "p1")
    assert row["status"] == "superseded"
    assert row["error_message"] == (
        f"{inbox_items.APPROVAL_INVALIDATED_PREFIX}superseded by newer modification"
    )
    assert row["processed_at"] == _T1
    assert row["retry_count"] == 2  # supersession burns no retry


@pytest.mark.asyncio
async def test_supersede_leaves_dispatching_rows_alone(db):
    await inbox_items.create(
        db,
        id="d1",
        file_path=_FILE,
        content_hash="h",
        status="processing",
        created_at=_T0,
        error_message=f"{inbox_items.DISPATCHING_PREFIX}req-1",
    )
    assert await inbox_items.supersede_parked_rows(db, _FILE, processed_at=_T1) == 0
    assert (await inbox_items.get_by_id(db, "d1"))["status"] == "processing"


@pytest.mark.asyncio
async def test_superseded_row_is_no_longer_live_for_its_approval(db):
    await _parked(db, "p1")
    assert await inbox_items.count_live_rows_for_approval(db, "req-1") == 1
    await inbox_items.supersede_parked_rows(db, _FILE, processed_at=_T1)
    assert await inbox_items.count_live_rows_for_approval(db, "req-1") == 0


@pytest.mark.asyncio
async def test_get_all_known_ignores_superseded_rows(db):
    """A superseded row never supplies a known hash — not even when it is the
    newest row for the file, and not when its retry budget is exhausted."""
    await inbox_items.create(
        db,
        id="c1",
        file_path=_FILE,
        content_hash="completed-hash",
        status="completed",
        created_at=_T0,
    )
    await _parked(db, "p1", created_at=_T1, retry_count=3)
    await inbox_items.supersede_parked_rows(db, _FILE, processed_at=_T1)
    known = await inbox_items.get_all_known(db, max_retries=3)
    assert known[_FILE] == "completed-hash"


@pytest.mark.asyncio
async def test_get_all_known_lone_superseded_row_leaves_file_unknown(db):
    """Unchanged behaviour: before this status existed the row was a retriable
    'failed' row and invisible to the scan; it stays invisible."""
    await _parked(db, "p1")
    await inbox_items.supersede_parked_rows(db, _FILE, processed_at=_T1)
    assert _FILE not in await inbox_items.get_all_known(db)


@pytest.mark.asyncio
async def test_superseded_row_at_retry_cap_is_not_handled_batch_content(db):
    """Behaviour change: a parked row at the retry cap that gets superseded was
    counted as 'handled' while supersession wrote 'failed', so its never-evaluated
    items were dropped from later deltas. It is no longer handled."""
    await inbox_items.create(
        db,
        id="p1",
        file_path=_FILE,
        content_hash="h",
        status="processing",
        created_at=_T0,
        batch_items="https://example.com/never-evaluated",
        error_message=f"{inbox_items.AWAITING_APPROVAL_PREFIX}req-1",
        retry_count=3,
    )
    # Control: a genuinely retry-exhausted failed row IS handled.
    await inbox_items.create(
        db,
        id="f1",
        file_path=_FILE,
        content_hash="h",
        status="failed",
        created_at=_T0,
        batch_items="https://example.com/exhausted",
        retry_count=3,
    )
    await inbox_items.supersede_parked_rows(db, _FILE, processed_at=_T1)
    handled = await inbox_items.get_handled_batch_content(db, _FILE, max_retries=3)
    assert handled == ["https://example.com/exhausted"]


@pytest.mark.asyncio
async def test_superseded_row_is_never_a_retry_candidate(db):
    await _parked(db, "p1")
    await inbox_items.supersede_parked_rows(db, _FILE, processed_at=_T1)
    assert await inbox_items.get_retriable_failed(db, _FILE) is None
    assert await inbox_items.get_retriable_failed_rows(db, _FILE) == []
    assert await inbox_items.get_retriable_failure_files(db) == []


@pytest.mark.asyncio
async def test_update_status_superseded_does_not_increment_retry(db):
    await _parked(db, "p1", retry_count=1)
    await inbox_items.update_status(
        db,
        "p1",
        status="superseded",
        processed_at=_T1,
        error_message=f"{inbox_items.APPROVAL_INVALIDATED_PREFIX}content changed",
    )
    row = await inbox_items.get_by_id(db, "p1")
    assert row["status"] == "superseded"
    assert row["retry_count"] == 1


@pytest.mark.asyncio
async def test_status_check_still_rejects_unknown_values(db):
    with pytest.raises(sqlite3.IntegrityError):
        await inbox_items.create(
            db,
            id="x",
            file_path=_FILE,
            content_hash="h",
            status="bogus",
            created_at=_T0,
        )
