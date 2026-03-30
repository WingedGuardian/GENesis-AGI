"""Tests for deferred_work extended CRUD: query_failed."""

from __future__ import annotations

import pytest

from genesis.db.crud import deferred_work as crud

_BASE = dict(
    work_type="reflection",
    priority=30,
    payload_json='{"key": "val"}',
    deferred_at="2026-03-14T10:00:00",
    deferred_reason="cloud_down",
    created_at="2026-03-14T10:00:00",
)


class TestQueryFailed:
    @pytest.mark.asyncio
    async def test_returns_items_with_error_message(self, db):
        rid = await crud.create(db, id="dw-err", **_BASE)
        await crud.update_status(db, rid, status="completed", error_message="timeout")
        rows = await crud.query_failed(db)
        assert len(rows) == 1
        assert rows[0]["error_message"] == "timeout"

    @pytest.mark.asyncio
    async def test_returns_expired_items(self, db):
        await crud.create(db, id="dw-exp", **_BASE, staleness_policy="discard")
        await crud.expire_by_policy(db, now_iso="2026-03-14T12:00:00")
        rows = await crud.query_failed(db)
        assert len(rows) == 1
        assert rows[0]["status"] == "expired"

    @pytest.mark.asyncio
    async def test_excludes_healthy_pending(self, db):
        await crud.create(db, id="dw-ok", **_BASE)
        rows = await crud.query_failed(db)
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_since_filter(self, db):
        await crud.create(db, id="dw-old", **{**_BASE, "created_at": "2026-03-13T10:00:00"})
        await crud.update_status(db, "dw-old", status="discarded", error_message="x")
        await crud.create(db, id="dw-new", **{**_BASE, "created_at": "2026-03-14T10:00:00"})
        await crud.update_status(db, "dw-new", status="discarded", error_message="y")
        rows = await crud.query_failed(db, since="2026-03-14T00:00:00")
        assert len(rows) == 1
        assert rows[0]["id"] == "dw-new"

    @pytest.mark.asyncio
    async def test_work_type_filter(self, db):
        await crud.create(db, id="dw-a", **_BASE)
        await crud.update_status(db, "dw-a", status="discarded", error_message="e")
        await crud.create(db, id="dw-b", **{**_BASE, "work_type": "outreach"})
        await crud.update_status(db, "dw-b", status="discarded", error_message="e")
        rows = await crud.query_failed(db, work_type="outreach")
        assert len(rows) == 1
        assert rows[0]["id"] == "dw-b"
