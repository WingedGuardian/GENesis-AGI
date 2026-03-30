"""Tests for genesis.autonomy.approval.ApprovalManager."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.autonomy.approval import ApprovalManager
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def manager(db):
    return ApprovalManager(db=db)


async def _create_request(mgr: ApprovalManager, **kwargs) -> str:
    defaults = {
        "action_type": "test_action",
        "action_class": "reversible",
        "description": "test description",
    }
    defaults.update(kwargs)
    return await mgr.request_approval(**defaults)


# ------------------------------------------------------------------
# Basic lifecycle
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_creates_entry(db):
    mgr = ApprovalManager(db=db)
    req_id = await _create_request(mgr)
    assert req_id
    row = await mgr.get_by_id(req_id)
    assert row is not None
    assert row["id"] == req_id


@pytest.mark.asyncio
async def test_request_pending_status(db):
    mgr = ApprovalManager(db=db)
    req_id = await _create_request(mgr)
    row = await mgr.get_by_id(req_id)
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_resolve_approved(db):
    mgr = ApprovalManager(db=db)
    req_id = await _create_request(mgr)
    ok = await mgr.resolve(req_id, status="approved")
    assert ok is True
    row = await mgr.get_by_id(req_id)
    assert row["status"] == "approved"


@pytest.mark.asyncio
async def test_resolve_rejected(db):
    mgr = ApprovalManager(db=db)
    req_id = await _create_request(mgr)
    ok = await mgr.resolve(req_id, status="rejected")
    assert ok is True
    row = await mgr.get_by_id(req_id)
    assert row["status"] == "rejected"


@pytest.mark.asyncio
async def test_cancel_sets_cancelled(db):
    mgr = ApprovalManager(db=db)
    req_id = await _create_request(mgr)
    ok = await mgr.cancel(req_id)
    assert ok is True
    row = await mgr.get_by_id(req_id)
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_resolve_nonexistent_returns_false(db):
    mgr = ApprovalManager(db=db)
    ok = await mgr.resolve("nonexistent-id", status="approved")
    assert ok is False


@pytest.mark.asyncio
async def test_expire_timed_out(db):
    mgr = ApprovalManager(db=db)
    req_id = await _create_request(mgr, timeout_seconds=3600)
    # Manually set timeout_at to the past
    await db.execute(
        "UPDATE approval_requests SET timeout_at = '2020-01-01T00:00:00' WHERE id = ?",
        (req_id,),
    )
    await db.commit()
    expired = await mgr.expire_timed_out()
    assert expired == 1
    row = await mgr.get_by_id(req_id)
    assert row["status"] == "expired"


@pytest.mark.asyncio
async def test_no_timeout_never_expires(db):
    mgr = ApprovalManager(db=db)
    await _create_request(mgr, timeout_seconds=None)
    expired = await mgr.expire_timed_out()
    assert expired == 0


@pytest.mark.asyncio
async def test_get_pending_returns_only_pending(db):
    mgr = ApprovalManager(db=db)
    id1 = await _create_request(mgr)
    id2 = await _create_request(mgr)
    await mgr.resolve(id1, status="approved")
    pending = await mgr.get_pending()
    pending_ids = [r["id"] for r in pending]
    assert id2 in pending_ids
    assert id1 not in pending_ids


@pytest.mark.asyncio
async def test_classifier_timeout_lookup(db):
    classifier = MagicMock()
    classifier.get_timeout.return_value = 120
    mgr = ApprovalManager(db=db, classifier=classifier)
    req_id = await _create_request(mgr)
    classifier.get_timeout.assert_called_once_with("test_action")
    row = await mgr.get_by_id(req_id)
    assert row["timeout_at"] is not None


@pytest.mark.asyncio
async def test_event_emitted_on_request(db):
    event_bus = AsyncMock()
    mgr = ApprovalManager(db=db, event_bus=event_bus)
    await _create_request(mgr)
    event_bus.emit.assert_called_once()
