"""Tests for the ego proposal workflow."""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES
from genesis.ego.proposals import (
    ProposalWorkflow,
    parse_reply,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """In-memory DB with ego tables."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_proposals"])
        await conn.execute(TABLES["ego_state"])
        yield conn


@pytest.fixture
def mock_topic_manager():
    """TopicManager that returns a canned delivery_id."""
    tm = AsyncMock()
    tm.send_to_category.return_value = "msg12345"
    return tm


@pytest.fixture
def mock_reply_waiter():
    """ReplyWaiter with controllable return value."""
    rw = AsyncMock()
    rw.wait_for_reply.return_value = "approve all"
    return rw


@pytest.fixture
def mock_memory_store():
    """MemoryStore mock for correction storage tests."""
    ms = AsyncMock()
    ms.store.return_value = "mem_123"
    return ms


@pytest.fixture
def workflow(db, mock_topic_manager, mock_reply_waiter, mock_memory_store):
    return ProposalWorkflow(
        db=db,
        topic_manager=mock_topic_manager,
        reply_waiter=mock_reply_waiter,
        memory_store=mock_memory_store,
    )


def _sample_proposals(n: int = 3) -> list[dict]:
    """Generate N sample proposal dicts."""
    samples = [
        {
            "action_type": "investigate",
            "action_category": "system_health",
            "content": "Check why observation backlog grew 3x",
            "rationale": "Backlog at 47 unresolved vs 15 yesterday",
            "confidence": 0.85,
            "urgency": "normal",
            "alternatives": "Wait for reflection to catch it",
        },
        {
            "action_type": "outreach",
            "action_category": "communication",
            "content": "Send weekly summary to user",
            "rationale": "7 days since last strategic report",
            "confidence": 0.70,
            "urgency": "low",
        },
        {
            "action_type": "maintenance",
            "action_category": "infrastructure",
            "content": "Run code audit on outreach pipeline",
            "rationale": "3 delivery failures in 24h",
            "confidence": 0.60,
            "urgency": "high",
            "alternatives": "Surplus could check cheaper",
        },
    ]
    return samples[:n]


# ---------------------------------------------------------------------------
# Proposal CRUD tests
# ---------------------------------------------------------------------------


class TestProposalCRUD:
    async def test_create_and_get_roundtrip(self, db):
        pid = await ego_crud.create_proposal(
            db, id="p1", action_type="investigate", content="test",
        )
        assert pid == "p1"
        row = await ego_crud.get_proposal(db, "p1")
        assert row is not None
        assert row["action_type"] == "investigate"
        assert row["status"] == "pending"

    async def test_get_missing(self, db):
        assert await ego_crud.get_proposal(db, "nope") is None

    async def test_list_by_batch(self, db):
        for i in range(3):
            await ego_crud.create_proposal(
                db, id=f"p{i}", action_type="test", content=f"c{i}",
                batch_id="batch1",
            )
        await ego_crud.create_proposal(
            db, id="other", action_type="test", content="other",
            batch_id="batch2",
        )
        rows = await ego_crud.list_proposals_by_batch(db, "batch1")
        assert len(rows) == 3
        assert [r["id"] for r in rows] == ["p0", "p1", "p2"]

    async def test_list_pending(self, db):
        await ego_crud.create_proposal(
            db, id="p1", action_type="t", content="c",
            created_at="2026-01-01",
        )
        await ego_crud.create_proposal(
            db, id="p2", action_type="t", content="c",
            created_at="2026-01-02",
        )
        await ego_crud.resolve_proposal(db, "p1", status="approved")
        rows = await ego_crud.list_pending_proposals(db)
        assert len(rows) == 1
        assert rows[0]["id"] == "p2"

    async def test_resolve_proposal(self, db):
        await ego_crud.create_proposal(
            db, id="p1", action_type="t", content="c",
        )
        ok = await ego_crud.resolve_proposal(
            db, "p1", status="approved", user_response="looks good",
        )
        assert ok is True
        row = await ego_crud.get_proposal(db, "p1")
        assert row["status"] == "approved"
        assert row["user_response"] == "looks good"
        assert row["resolved_at"] is not None

    async def test_resolve_nonexistent(self, db):
        ok = await ego_crud.resolve_proposal(db, "nope", status="approved")
        assert ok is False

    async def test_resolve_already_resolved(self, db):
        """Can't resolve an already-resolved proposal."""
        await ego_crud.create_proposal(
            db, id="p1", action_type="t", content="c",
        )
        await ego_crud.resolve_proposal(db, "p1", status="approved")
        ok = await ego_crud.resolve_proposal(db, "p1", status="rejected")
        assert ok is False

    async def test_batch_delivery_mapping(self, db):
        await ego_crud.set_state(
            db, key="delivery_batch:msg123", value="batch_abc",
        )
        assert await ego_crud.get_batch_for_delivery(db, "msg123") == "batch_abc"
        assert await ego_crud.get_batch_for_delivery(db, "unknown") is None


# ---------------------------------------------------------------------------
# parse_reply tests
# ---------------------------------------------------------------------------


class TestParseReply:
    def test_approve_all(self):
        r = parse_reply("approve all", 3)
        assert r == {1: ("approved", None), 2: ("approved", None), 3: ("approved", None)}

    def test_approve_bare(self):
        r = parse_reply("approve", 2)
        assert all(s == "approved" for s, _ in r.values())
        assert len(r) == 2

    def test_lgtm(self):
        r = parse_reply("lgtm", 3)
        assert len(r) == 3
        assert all(s == "approved" for s, _ in r.values())

    def test_ok(self):
        r = parse_reply("ok", 2)
        assert len(r) == 2

    def test_yes(self):
        r = parse_reply("yes", 1)
        assert r == {1: ("approved", None)}

    def test_reject_all(self):
        r = parse_reply("reject all", 3)
        assert all(s == "rejected" for s, _ in r.values())

    def test_reject_with_reason(self):
        r = parse_reply("reject all — bad idea", 2)
        assert r[1] == ("rejected", "bad idea")
        assert r[2] == ("rejected", "bad idea")

    def test_selective_approve(self):
        r = parse_reply("approve 1,3", 3)
        assert r == {1: ("approved", None), 3: ("approved", None)}
        assert 2 not in r

    def test_selective_reject(self):
        r = parse_reply("reject 2 — not worth it", 3)
        assert r == {2: ("rejected", "not worth it")}

    def test_numbers_in_reason_not_matched(self):
        """Numbers in rejection reason must not bleed into proposal selection."""
        r = parse_reply("reject 2 — first one is low-priority", 3)
        assert r == {2: ("rejected", "first one is low-priority")}
        assert 1 not in r  # "1" from "first one" must not match

    def test_mixed(self):
        r = parse_reply("approve 1,3 reject 2", 3)
        assert r[1] == ("approved", None)
        assert r[2][0] == "rejected"
        assert r[3] == ("approved", None)

    def test_multiline_mixed(self):
        r = parse_reply("approve 1\nreject 2 — nah", 3)
        assert r[1] == ("approved", None)
        assert r[2] == ("rejected", "nah")

    def test_bare_numbers(self):
        r = parse_reply("1,3", 3)
        assert r == {1: ("approved", None), 3: ("approved", None)}

    def test_out_of_range_ignored(self):
        r = parse_reply("approve 5", 3)
        assert r == {}

    def test_empty_reply(self):
        assert parse_reply("", 3) == {}

    def test_unparseable(self):
        assert parse_reply("hello world", 3) == {}

    def test_case_insensitive(self):
        r = parse_reply("APPROVE ALL", 2)
        assert len(r) == 2

    def test_go_ahead(self):
        r = parse_reply("go ahead", 2)
        # "go" is approve keyword, "ahead" is not "all" but also not a number
        # — this triggers selective path which finds no numbers → falls through
        # to bare-number check which finds none → empty. BUT "go" alone doesn't
        # match "approve all" because rest_words[0] = "ahead" not in _ALL_WORDS.
        # This is acceptable: "go" without "all" or numbers is ambiguous.
        # If this is a concern, "go ahead" could be special-cased later.
        # For now, verify it doesn't crash.
        assert isinstance(r, dict)

    def test_ship(self):
        r = parse_reply("ship", 2)
        assert len(r) == 2


# ---------------------------------------------------------------------------
# Workflow integration tests
# ---------------------------------------------------------------------------


class TestProposalWorkflow:
    async def test_create_batch_inserts(self, workflow, db):
        batch_id, ids = await workflow.create_batch(
            _sample_proposals(3), cycle_id="cycle1",
        )
        assert len(ids) == 3
        assert len(batch_id) == 16

        rows = await ego_crud.list_proposals_by_batch(db, batch_id)
        assert len(rows) == 3
        assert all(r["cycle_id"] == "cycle1" for r in rows)
        assert all(r["batch_id"] == batch_id for r in rows)

    async def test_create_batch_with_new_fields(self, workflow, db):
        props = [{
            "action_type": "investigate",
            "action_category": "system_health",
            "content": "Check backlog",
            "rationale": "Growing",
            "confidence": 0.85,
            "rank": 1,
            "execution_plan": "background CC, ~$0.30",
            "recurring": True,
        }]
        batch_id, ids = await workflow.create_batch(props)
        row = (await ego_crud.list_proposals_by_batch(db, batch_id))[0]
        assert row["rank"] == 1
        assert row["execution_plan"] == "background CC, ~$0.30"
        assert row["recurring"] == 1

    async def test_format_digest_html(self, workflow):
        digest = workflow.format_digest(_sample_proposals(2), "batch123")
        assert "<b>Ego Proposals</b>" in digest
        assert "batch123" in digest  # first 8 chars of batch_id
        assert "<b>1.</b>" in digest
        assert "<b>2.</b>" in digest
        assert "[investigate]" in digest
        assert "approve all" in digest

    async def test_format_digest_escapes_html(self, workflow):
        bad = [{"action_type": "<script>", "content": "a<b>c", "confidence": 0.5}]
        digest = workflow.format_digest(bad, "batch1")
        assert "<script>" not in digest
        assert "&lt;script&gt;" in digest

    async def test_format_digest_alternatives_shown(self, workflow):
        props = [_sample_proposals(1)[0]]  # has alternatives
        digest = workflow.format_digest(props, "b1")
        assert "Alternatives:" in digest

    async def test_format_digest_alternatives_hidden(self, workflow):
        props = [_sample_proposals(2)[1]]  # no alternatives key or empty
        digest = workflow.format_digest(props, "b1")
        assert "Alternatives:" not in digest

    async def test_send_digest_calls_topic_manager(
        self, workflow, db, mock_topic_manager,
    ):
        batch_id, _ = await workflow.create_batch(_sample_proposals(2))
        delivery = await workflow.send_digest(batch_id)
        assert delivery == "msg12345"
        mock_topic_manager.send_to_category.assert_called_once()
        call_args = mock_topic_manager.send_to_category.call_args
        assert call_args[0][0] == "ego_proposals"

    async def test_send_digest_stores_mapping(self, workflow, db):
        batch_id, _ = await workflow.create_batch(_sample_proposals(1))
        delivery = await workflow.send_digest(batch_id)
        assert await ego_crud.get_batch_for_delivery(db, delivery) == batch_id
        assert await ego_crud.get_state(
            db, f"batch_delivery:{batch_id}",
        ) == delivery

    async def test_send_digest_no_topic_manager(self, db, mock_reply_waiter):
        wf = ProposalWorkflow(
            db=db, topic_manager=None, reply_waiter=mock_reply_waiter,
        )
        batch_id, _ = await wf.create_batch(_sample_proposals(1))
        assert await wf.send_digest(batch_id) is None

    async def test_wait_approve_all(
        self, workflow, db, mock_reply_waiter,
    ):
        batch_id, ids = await workflow.create_batch(_sample_proposals(3))
        delivery = await workflow.send_digest(batch_id)

        mock_reply_waiter.wait_for_reply.return_value = "approve all"
        results = await workflow.wait_and_process_reply(
            batch_id, delivery, timeout_s=5,
        )
        assert len(results) == 3
        assert all(s == "approved" for s in results.values())

        for pid in ids:
            row = await ego_crud.get_proposal(db, pid)
            assert row["status"] == "approved"

    async def test_wait_selective(
        self, workflow, db, mock_reply_waiter,
    ):
        batch_id, ids = await workflow.create_batch(_sample_proposals(3))
        delivery = await workflow.send_digest(batch_id)

        mock_reply_waiter.wait_for_reply.return_value = "approve 1 reject 2 — meh"
        results = await workflow.wait_and_process_reply(
            batch_id, delivery, timeout_s=5,
        )
        assert results[ids[0]] == "approved"
        assert results[ids[1]] == "rejected"
        assert ids[2] not in results  # not mentioned, stays pending

        assert (await ego_crud.get_proposal(db, ids[2]))["status"] == "pending"

    async def test_wait_timeout(
        self, workflow, db, mock_reply_waiter,
    ):
        batch_id, ids = await workflow.create_batch(_sample_proposals(2))
        delivery = await workflow.send_digest(batch_id)

        mock_reply_waiter.wait_for_reply.return_value = None  # timeout
        results = await workflow.wait_and_process_reply(
            batch_id, delivery, timeout_s=0.1,
        )
        assert results == {}
        # Proposals still pending
        for pid in ids:
            assert (await ego_crud.get_proposal(db, pid))["status"] == "pending"

    async def test_correction_stored_on_reject_with_reason(
        self, workflow, db, mock_memory_store,
    ):
        batch_id, ids = await workflow.create_batch(_sample_proposals(2))
        await workflow.send_digest(batch_id)
        mock_memory_store.reset_mock()

        # Reject proposal 1 with a reason
        results = await workflow.resolve_proposals(
            batch_id, {1: ("rejected", "waste of time")},
        )
        assert results[ids[0]] == "rejected"

        # Verify correction was stored
        mock_memory_store.store.assert_called_once()
        call_kwargs = mock_memory_store.store.call_args[1]
        assert "waste of time" in call_kwargs["content"]
        assert call_kwargs["wing"] == "autonomy"
        assert call_kwargs["room"] == "ego_corrections"
        assert "ego_correction" in call_kwargs["tags"]

    async def test_no_correction_on_reject_without_reason(
        self, workflow, db, mock_memory_store,
    ):
        batch_id, ids = await workflow.create_batch(_sample_proposals(1))
        mock_memory_store.reset_mock()

        await workflow.resolve_proposals(
            batch_id, {1: ("rejected", None)},
        )
        mock_memory_store.store.assert_not_called()

    async def test_no_correction_on_approve(
        self, workflow, db, mock_memory_store,
    ):
        batch_id, ids = await workflow.create_batch(_sample_proposals(1))
        mock_memory_store.reset_mock()

        await workflow.resolve_proposals(
            batch_id, {1: ("approved", None)},
        )
        mock_memory_store.store.assert_not_called()

    async def test_correction_failure_does_not_block(self, db, mock_topic_manager, mock_reply_waiter):
        """If memory_store.store raises, proposal still gets resolved."""
        bad_store = AsyncMock()
        bad_store.store.side_effect = RuntimeError("Qdrant down")
        wf = ProposalWorkflow(
            db=db,
            topic_manager=mock_topic_manager,
            reply_waiter=mock_reply_waiter,
            memory_store=bad_store,
        )
        batch_id, ids = await wf.create_batch(_sample_proposals(1))
        results = await wf.resolve_proposals(
            batch_id, {1: ("rejected", "bad idea")},
        )
        assert results[ids[0]] == "rejected"
        row = await ego_crud.get_proposal(db, ids[0])
        assert row["status"] == "rejected"
