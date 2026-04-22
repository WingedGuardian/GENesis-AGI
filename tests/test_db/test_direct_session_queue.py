"""Tests for direct_session_queue CRUD operations."""

from __future__ import annotations

import json

import pytest

from genesis.db.crud import direct_session_queue as dsq


class TestEnqueue:
    @pytest.mark.asyncio
    async def test_enqueue_returns_prefixed_id(self, db):
        qid = await dsq.enqueue(db, prompt="hello world")
        assert qid.startswith("dsq-")

    @pytest.mark.asyncio
    async def test_enqueue_stores_payload(self, db):
        qid = await dsq.enqueue(
            db,
            prompt="investigate post removals",
            profile="observe",
            model="sonnet",
            effort="high",
            timeout_s=600,
            notify=True,
            caller_context="mcp_tool",
        )
        row = await dsq.get_by_id(db, qid)
        assert row is not None
        assert row["status"] == "pending"

        payload = json.loads(row["payload_json"])
        assert payload["prompt"] == "investigate post removals"
        assert payload["profile"] == "observe"
        assert payload["model"] == "sonnet"
        assert payload["timeout_s"] == 600
        assert payload["caller_context"] == "mcp_tool"

    @pytest.mark.asyncio
    async def test_enqueue_defaults(self, db):
        qid = await dsq.enqueue(db, prompt="test")
        row = await dsq.get_by_id(db, qid)
        payload = json.loads(row["payload_json"])
        assert payload["profile"] == "observe"
        assert payload["model"] == "sonnet"
        assert payload["effort"] == "high"
        assert payload["timeout_s"] == 900
        assert payload["notify"] is True
        assert payload["notify_on_failure_only"] is False


class TestClaimNext:
    @pytest.mark.asyncio
    async def test_claim_next_empty(self, db):
        row = await dsq.claim_next(db)
        assert row is None

    @pytest.mark.asyncio
    async def test_claim_next_returns_oldest(self, db):
        q1 = await dsq.enqueue(db, prompt="first")
        await dsq.enqueue(db, prompt="second")
        row = await dsq.claim_next(db)
        assert row is not None
        assert row["id"] == q1
        assert row["status"] == "claimed"
        assert row["claimed_at"] is not None

    @pytest.mark.asyncio
    async def test_claim_next_skips_claimed(self, db):
        q1 = await dsq.enqueue(db, prompt="first")
        q2 = await dsq.enqueue(db, prompt="second")
        await dsq.claim_next(db)  # claims q1
        row = await dsq.claim_next(db)
        assert row is not None
        assert row["id"] == q2

    @pytest.mark.asyncio
    async def test_claim_next_skips_dispatched(self, db):
        q1 = await dsq.enqueue(db, prompt="first")
        row = await dsq.claim_next(db)
        await dsq.mark_dispatched(db, q1, "sess-123")
        row = await dsq.claim_next(db)
        assert row is None


class TestMarkDispatched:
    @pytest.mark.asyncio
    async def test_mark_dispatched(self, db):
        qid = await dsq.enqueue(db, prompt="test")
        await dsq.claim_next(db)
        await dsq.mark_dispatched(db, qid, "sess-abc")

        row = await dsq.get_by_id(db, qid)
        assert row["status"] == "dispatched"
        assert row["session_id"] == "sess-abc"
        assert row["dispatched_at"] is not None


class TestMarkFailed:
    @pytest.mark.asyncio
    async def test_mark_failed(self, db):
        qid = await dsq.enqueue(db, prompt="test")
        await dsq.claim_next(db)
        await dsq.mark_failed(db, qid, "spawn error: invoker busy")

        row = await dsq.get_by_id(db, qid)
        assert row["status"] == "failed"
        assert row["error_message"] == "spawn error: invoker busy"


class TestRecoverStaleClaims:
    @pytest.mark.asyncio
    async def test_recover_stale_claims(self, db):
        qid = await dsq.enqueue(db, prompt="test")
        await dsq.claim_next(db)

        # With max_age_s=0, everything claimed is "stale"
        recovered = await dsq.recover_stale_claims(db, max_age_s=0)
        assert recovered == 1

        row = await dsq.get_by_id(db, qid)
        assert row["status"] == "pending"
        assert row["claimed_at"] is None

    @pytest.mark.asyncio
    async def test_recover_does_not_touch_dispatched(self, db):
        qid = await dsq.enqueue(db, prompt="test")
        await dsq.claim_next(db)
        await dsq.mark_dispatched(db, qid, "sess-123")

        recovered = await dsq.recover_stale_claims(db, max_age_s=0)
        assert recovered == 0

        row = await dsq.get_by_id(db, qid)
        assert row["status"] == "dispatched"


class TestCountPending:
    @pytest.mark.asyncio
    async def test_count_pending(self, db):
        assert await dsq.count_pending(db) == 0
        await dsq.enqueue(db, prompt="a")
        await dsq.enqueue(db, prompt="b")
        assert await dsq.count_pending(db) == 2

        # Claim one — no longer pending
        await dsq.claim_next(db)
        assert await dsq.count_pending(db) == 1
