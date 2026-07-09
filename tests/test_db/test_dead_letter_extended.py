"""Tests for dead_letter extended CRUD: query_recent."""

from __future__ import annotations

import pytest

from genesis.db.crud import dead_letter as crud

_BASE = dict(
    operation_type="llm_call",
    payload='{"messages": []}',
    target_provider="anthropic",
    failure_reason="rate_limited",
)


class TestQueryRecent:
    @pytest.mark.asyncio
    async def test_returns_all_statuses(self, db):
        await crud.create(db, id="dl-a", created_at="2026-03-14T10:00:00", status="pending", **_BASE)
        await crud.create(db, id="dl-b", created_at="2026-03-14T11:00:00", status="resolved", **_BASE)
        rows = await crud.query_recent(db)
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_since_filter(self, db):
        await crud.create(db, id="dl-old", created_at="2026-03-13T10:00:00", **_BASE)
        await crud.create(db, id="dl-new", created_at="2026-03-14T10:00:00", **_BASE)
        rows = await crud.query_recent(db, since="2026-03-14T00:00:00")
        assert len(rows) == 1
        assert rows[0]["id"] == "dl-new"

    @pytest.mark.asyncio
    async def test_provider_filter(self, db):
        await crud.create(db, id="dl-x", created_at="2026-03-14T10:00:00", **_BASE)
        await crud.create(
            db, id="dl-y", created_at="2026-03-14T10:00:00",
            operation_type="embed", payload="{}", target_provider="qdrant",
            failure_reason="timeout",
        )
        rows = await crud.query_recent(db, target_provider="qdrant")
        assert len(rows) == 1
        assert rows[0]["id"] == "dl-y"

    @pytest.mark.asyncio
    async def test_ordered_desc(self, db):
        await crud.create(db, id="dl-1", created_at="2026-03-14T09:00:00", **_BASE)
        await crud.create(db, id="dl-2", created_at="2026-03-14T11:00:00", **_BASE)
        rows = await crud.query_recent(db)
        assert rows[0]["id"] == "dl-2"

    @pytest.mark.asyncio
    async def test_limit(self, db):
        for i in range(5):
            await crud.create(db, id=f"dl-lim-{i}", created_at=f"2026-03-14T1{i}:00:00", **_BASE)
        rows = await crud.query_recent(db, limit=2)
        assert len(rows) == 2


class TestRecentProviderCounts:
    @pytest.mark.asyncio
    async def test_groups_by_provider_ordered_desc(self, db):
        for i in range(3):
            await crud.create(db, id=f"dl-a{i}", created_at=f"2026-03-14T1{i}:00:00", **_BASE)
        await crud.create(
            db, id="dl-q0", created_at="2026-03-14T10:30:00",
            operation_type="embed", payload="{}", target_provider="qdrant",
            failure_reason="timeout",
        )
        rows = await crud.recent_provider_counts(db, since="2026-03-14T00:00:00")
        assert rows[0] == ("anthropic", 3, "2026-03-14T12:00:00")
        assert rows[1] == ("qdrant", 1, "2026-03-14T10:30:00")

    @pytest.mark.asyncio
    async def test_window_respected(self, db):
        await crud.create(db, id="dl-old", created_at="2026-03-13T10:00:00", **_BASE)
        await crud.create(db, id="dl-new", created_at="2026-03-14T10:00:00", **_BASE)
        rows = await crud.recent_provider_counts(db, since="2026-03-14T00:00:00")
        assert rows == [("anthropic", 1, "2026-03-14T10:00:00")]

    @pytest.mark.asyncio
    async def test_counts_any_status(self, db):
        await crud.create(db, id="dl-p", created_at="2026-03-14T10:00:00", status="pending", **_BASE)
        await crud.create(db, id="dl-r", created_at="2026-03-14T11:00:00", status="replayed", **_BASE)
        await crud.create(db, id="dl-e", created_at="2026-03-14T12:00:00", status="expired", **_BASE)
        rows = await crud.recent_provider_counts(db, since="2026-03-14T00:00:00")
        assert rows == [("anthropic", 3, "2026-03-14T12:00:00")]

    @pytest.mark.asyncio
    async def test_all_sentinel_passes_through(self, db):
        await crud.create(
            db, id="dl-all", created_at="2026-03-14T10:00:00",
            operation_type="chain_exhausted:judge", payload="{}",
            target_provider="all", failure_reason="all_exhausted",
        )
        rows = await crud.recent_provider_counts(db, since="2026-03-14T00:00:00")
        assert rows == [("all", 1, "2026-03-14T10:00:00")]
