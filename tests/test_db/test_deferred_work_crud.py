"""Tests for deferred_work CRUD operations."""

from __future__ import annotations

import pytest

from genesis.db.crud import deferred_work as crud


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_id(self, db):
        result = await crud.create(
            db, id="dw-1", work_type="reflection", priority=30,
            payload_json='{"key": "val"}', deferred_at="2026-03-11T12:00:00",
            deferred_reason="cloud_down", created_at="2026-03-11T12:00:00",
        )
        assert result == "dw-1"

    @pytest.mark.asyncio
    async def test_create_with_optional_fields(self, db):
        await crud.create(
            db, id="dw-2", work_type="outreach", priority=60,
            payload_json='{}', deferred_at="2026-03-11T12:00:00",
            deferred_reason="rate_limited", created_at="2026-03-11T12:00:00",
            call_site_id="cs-1", staleness_policy="ttl", staleness_ttl_s=600,
        )
        items = await crud.query_pending(db)
        assert len(items) == 1
        assert items[0]["call_site_id"] == "cs-1"
        assert items[0]["staleness_policy"] == "ttl"
        assert items[0]["staleness_ttl_s"] == 600


class TestQueryPending:
    @pytest.mark.asyncio
    async def test_ordered_by_priority(self, db):
        await crud.create(
            db, id="dw-low", work_type="surplus", priority=80,
            payload_json='{}', deferred_at="2026-03-11T12:00:00",
            deferred_reason="r", created_at="2026-03-11T12:00:00",
        )
        await crud.create(
            db, id="dw-high", work_type="foreground", priority=10,
            payload_json='{}', deferred_at="2026-03-11T12:00:00",
            deferred_reason="r", created_at="2026-03-11T12:00:00",
        )
        items = await crud.query_pending(db)
        assert items[0]["id"] == "dw-high"

    @pytest.mark.asyncio
    async def test_filter_by_work_type(self, db):
        await crud.create(
            db, id="dw-a", work_type="reflection", priority=30,
            payload_json='{}', deferred_at="2026-03-11T12:00:00",
            deferred_reason="r", created_at="2026-03-11T12:00:00",
        )
        await crud.create(
            db, id="dw-b", work_type="surplus", priority=80,
            payload_json='{}', deferred_at="2026-03-11T12:00:00",
            deferred_reason="r", created_at="2026-03-11T12:00:00",
        )
        items = await crud.query_pending(db, work_type="reflection")
        assert len(items) == 1
        assert items[0]["work_type"] == "reflection"


class TestUpdateStatus:
    @pytest.mark.asyncio
    async def test_update_status(self, db):
        await crud.create(
            db, id="dw-1", work_type="test", priority=50,
            payload_json='{}', deferred_at="2026-03-11T12:00:00",
            deferred_reason="r", created_at="2026-03-11T12:00:00",
        )
        assert await crud.update_status(db, "dw-1", status="completed", completed_at="2026-03-11T13:00:00")
        assert await crud.count_pending(db) == 0

    @pytest.mark.asyncio
    async def test_update_nonexistent(self, db):
        assert not await crud.update_status(db, "nonexistent", status="completed")


class TestCountPending:
    @pytest.mark.asyncio
    async def test_count_empty(self, db):
        assert await crud.count_pending(db) == 0

    @pytest.mark.asyncio
    async def test_count_with_type_filter(self, db):
        await crud.create(
            db, id="dw-1", work_type="a", priority=50,
            payload_json='{}', deferred_at="2026-03-11T12:00:00",
            deferred_reason="r", created_at="2026-03-11T12:00:00",
        )
        await crud.create(
            db, id="dw-2", work_type="b", priority=50,
            payload_json='{}', deferred_at="2026-03-11T12:00:00",
            deferred_reason="r", created_at="2026-03-11T12:00:00",
        )
        assert await crud.count_pending(db, work_type="a") == 1
        assert await crud.count_pending(db) == 2


class TestExpireByPolicy:
    @pytest.mark.asyncio
    async def test_expire_refresh(self, db):
        await crud.create(
            db, id="dw-1", work_type="report", priority=70,
            payload_json='{}', deferred_at="2026-03-11T12:00:00",
            deferred_reason="r", created_at="2026-03-11T12:00:00",
            staleness_policy="refresh",
        )
        expired = await crud.expire_by_policy(db, now_iso="2026-03-11T12:00:01")
        assert expired == 1

    @pytest.mark.asyncio
    async def test_expire_discard(self, db):
        await crud.create(
            db, id="dw-1", work_type="surplus", priority=80,
            payload_json='{}', deferred_at="2026-03-11T12:00:00",
            deferred_reason="r", created_at="2026-03-11T12:00:00",
            staleness_policy="discard",
        )
        expired = await crud.expire_by_policy(db, now_iso="2026-03-11T12:00:01")
        assert expired == 1

    @pytest.mark.asyncio
    async def test_drain_not_expired(self, db):
        await crud.create(
            db, id="dw-1", work_type="test", priority=50,
            payload_json='{}', deferred_at="2026-03-11T12:00:00",
            deferred_reason="r", created_at="2026-03-11T12:00:00",
            staleness_policy="drain",
        )
        expired = await crud.expire_by_policy(db, now_iso="2026-03-12T12:00:00")
        assert expired == 0

    @pytest.mark.asyncio
    async def test_ttl_expired(self, db):
        await crud.create(
            db, id="dw-1", work_type="test", priority=50,
            payload_json='{}', deferred_at="2026-03-11T12:00:00",
            deferred_reason="r", created_at="2026-03-11T12:00:00",
            staleness_policy="ttl", staleness_ttl_s=300,
        )
        # 10 minutes later — past 5 minute TTL
        expired = await crud.expire_by_policy(db, now_iso="2026-03-11T12:10:00")
        assert expired == 1

    @pytest.mark.asyncio
    async def test_ttl_not_expired_yet(self, db):
        await crud.create(
            db, id="dw-1", work_type="test", priority=50,
            payload_json='{}', deferred_at="2026-03-11T12:00:00",
            deferred_reason="r", created_at="2026-03-11T12:00:00",
            staleness_policy="ttl", staleness_ttl_s=300,
        )
        # 2 minutes later — within 5 minute TTL
        expired = await crud.expire_by_policy(db, now_iso="2026-03-11T12:02:00")
        assert expired == 0


class TestExpireStuckProcessing:
    @pytest.mark.asyncio
    async def test_resets_old_processing_to_pending(self, db):
        await crud.create(
            db, id="dw-1", work_type="reflection", priority=30,
            payload_json='{}', deferred_at="2026-03-11T10:00:00",
            deferred_reason="r", created_at="2026-03-11T10:00:00",
        )
        await crud.update_status(
            db, "dw-1", status="processing",
            last_attempt_at="2026-03-11T10:00:00",
        )
        count = await crud.expire_stuck_processing(db, max_age_hours=2)
        assert count == 1
        items = await crud.query_pending(db)
        assert len(items) == 1
        assert items[0]["id"] == "dw-1"

    @pytest.mark.asyncio
    async def test_does_not_reset_recent_processing(self, db):
        await crud.create(
            db, id="dw-1", work_type="reflection", priority=30,
            payload_json='{}', deferred_at="2026-03-11T12:00:00",
            deferred_reason="r", created_at="2026-03-11T12:00:00",
        )
        from datetime import UTC, datetime
        now = datetime.now(UTC).isoformat()
        await crud.update_status(
            db, "dw-1", status="processing", last_attempt_at=now,
        )
        count = await crud.expire_stuck_processing(db, max_age_hours=2)
        assert count == 0

    @pytest.mark.asyncio
    async def test_ignores_pending_and_completed(self, db):
        await crud.create(
            db, id="dw-p", work_type="r", priority=30,
            payload_json='{}', deferred_at="2026-03-11T10:00:00",
            deferred_reason="r", created_at="2026-03-11T10:00:00",
        )
        await crud.create(
            db, id="dw-c", work_type="r", priority=30,
            payload_json='{}', deferred_at="2026-03-11T10:00:00",
            deferred_reason="r", created_at="2026-03-11T10:00:00",
        )
        await crud.update_status(db, "dw-c", status="completed")
        count = await crud.expire_stuck_processing(db, max_age_hours=2)
        assert count == 0
