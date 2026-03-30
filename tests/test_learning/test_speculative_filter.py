"""Tests for speculative claim filtering and expiry."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db import schema
from genesis.learning.speculative_filter import expire_stale_claims, filter_speculative


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        for ddl in schema.TABLES.values():
            await conn.execute(ddl)
        await conn.commit()
        yield conn


class TestFilterSpeculative:
    @pytest.mark.asyncio
    async def test_removes_speculative_items(self):
        items = [
            {"id": "1", "content": "confirmed", "speculative": False},
            {"id": "2", "content": "guess", "speculative": True},
            {"id": "3", "content": "also confirmed"},
        ]
        result = await filter_speculative(items)
        assert len(result) == 2
        assert all(not r.get("speculative", False) for r in result)

    @pytest.mark.asyncio
    async def test_empty_list(self):
        assert await filter_speculative([]) == []

    @pytest.mark.asyncio
    async def test_all_speculative(self):
        items = [{"speculative": True}, {"speculative": 1}]
        result = await filter_speculative(items)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_missing_key_treated_as_non_speculative(self):
        items = [{"id": "1"}]
        result = await filter_speculative(items)
        assert len(result) == 1


class TestExpireStaleClaims:
    async def _insert_claim(self, db, *, claim_id=None, expiry_offset_hours=0, evidence_count=0):
        cid = claim_id or str(uuid.uuid4())
        now = datetime.now(UTC)
        expiry = (now + timedelta(hours=expiry_offset_hours)).isoformat()
        await db.execute(
            "INSERT INTO speculative_claims (id, claim, speculative, evidence_count, "
            "hypothesis_expiry, created_at) VALUES (?, ?, 1, ?, ?, ?)",
            (cid, "test claim", evidence_count, expiry, now.isoformat()),
        )
        await db.commit()
        return cid

    @pytest.mark.asyncio
    async def test_expires_past_due_no_evidence(self, db):
        cid = await self._insert_claim(db, expiry_offset_hours=-1, evidence_count=0)
        count = await expire_stale_claims(db)
        assert count == 1
        cursor = await db.execute("SELECT archived_at FROM speculative_claims WHERE id = ?", (cid,))
        row = dict(await cursor.fetchone())
        assert row["archived_at"] is not None

    @pytest.mark.asyncio
    async def test_does_not_expire_with_evidence(self, db):
        await self._insert_claim(db, expiry_offset_hours=-1, evidence_count=2)
        count = await expire_stale_claims(db)
        assert count == 0

    @pytest.mark.asyncio
    async def test_does_not_expire_future_claims(self, db):
        await self._insert_claim(db, expiry_offset_hours=24, evidence_count=0)
        count = await expire_stale_claims(db)
        assert count == 0

    @pytest.mark.asyncio
    async def test_empty_table(self, db):
        count = await expire_stale_claims(db)
        assert count == 0

    @pytest.mark.asyncio
    async def test_mixed_claims(self, db):
        await self._insert_claim(db, expiry_offset_hours=-2, evidence_count=0)  # expires
        await self._insert_claim(db, expiry_offset_hours=-1, evidence_count=3)  # has evidence
        await self._insert_claim(db, expiry_offset_hours=10, evidence_count=0)  # future
        count = await expire_stale_claims(db)
        assert count == 1
