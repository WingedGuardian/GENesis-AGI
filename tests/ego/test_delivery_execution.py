"""Tests for ego Layers 4 (Delivery) and 5 (Execution) improvements."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from genesis.db.crud import ego as ego_crud
from genesis.ego.proposals import (
    ProposalWorkflow,
    _sort_proposals,
    parse_proposal_decisions,
)

# -- Layer 4: Delivery tests --


class TestProposalExpiry:
    @pytest.mark.asyncio
    async def test_expire_stale_proposals(self, db):
        """Proposals past expires_at are marked expired."""
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        await ego_crud.create_proposal(
            db,
            id="exp1",
            action_type="investigate",
            content="Old proposal",
            expires_at=past,
        )
        expired = await ego_crud.expire_stale_proposals(db)
        assert expired == 1
        prop = await ego_crud.get_proposal(db, "exp1")
        assert prop["status"] == "expired"
        assert prop["resolved_at"] is not None

    @pytest.mark.asyncio
    async def test_expire_skips_unexpired(self, db):
        """Proposals with future expires_at are not expired."""
        future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        await ego_crud.create_proposal(
            db,
            id="fut1",
            action_type="investigate",
            content="Future proposal",
            expires_at=future,
        )
        expired = await ego_crud.expire_stale_proposals(db)
        assert expired == 0
        prop = await ego_crud.get_proposal(db, "fut1")
        assert prop["status"] == "pending"

    @pytest.mark.asyncio
    async def test_expire_updates_journal(self, db):
        """Expiry also updates matching intervention_journal entries."""
        from genesis.db.crud import intervention_journal as journal_crud

        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        await ego_crud.create_proposal(
            db,
            id="exp_j",
            action_type="dispatch",
            content="Journal test",
            expires_at=past,
        )
        # Create matching journal entry
        await journal_crud.create(
            db,
            ego_source="user_ego_cycle",
            proposal_id="exp_j",
            cycle_id="cycle1",
            action_type="dispatch",
            action_summary="Journal test",
        )
        expired = await ego_crud.expire_stale_proposals(db)
        assert expired == 1

        journal = await journal_crud.get_by_proposal(db, "exp_j")
        assert journal is not None
        assert journal["outcome_status"] == "expired"


class TestPrioritySorting:
    def test_urgency_ordering(self):
        """Critical proposals sort before normal ones."""
        proposals = [
            {"urgency": "normal", "confidence": 0.9, "content": "normal"},
            {"urgency": "critical", "confidence": 0.5, "content": "critical"},
            {"urgency": "high", "confidence": 0.7, "content": "high"},
        ]
        sorted_p = _sort_proposals(proposals)
        assert sorted_p[0]["urgency"] == "critical"
        assert sorted_p[1]["urgency"] == "high"
        assert sorted_p[2]["urgency"] == "normal"

    def test_confidence_within_urgency(self):
        """Within same urgency, higher confidence sorts first."""
        proposals = [
            {"urgency": "high", "confidence": 0.5, "content": "low conf"},
            {"urgency": "high", "confidence": 0.9, "content": "high conf"},
        ]
        sorted_p = _sort_proposals(proposals)
        assert sorted_p[0]["confidence"] == 0.9
        assert sorted_p[1]["confidence"] == 0.5


class TestCrossBatchParser:
    def test_approve_all_pending(self):
        """'approve all pending' returns cross-batch sentinel -1."""
        result = parse_proposal_decisions("approve all pending")
        assert -1 in result
        assert result[-1] == ("approved", None)

    def test_reject_all_pending(self):
        """'reject all pending' returns cross-batch sentinel -1."""
        result = parse_proposal_decisions("reject all pending")
        assert -1 in result
        assert result[-1] == ("rejected", None)

    def test_approve_all_still_works(self):
        """'approve all' (batch-scoped) still returns sentinel 0."""
        result = parse_proposal_decisions("approve all")
        assert 0 in result
        assert result[0] == ("approved", None)
        assert -1 not in result

    def test_numbered_still_works(self):
        """'1 approve, 2 reject' still works as before."""
        result = parse_proposal_decisions("1 approve, 2 reject: bad idea")
        assert result[1] == ("approved", None)
        assert result[2] == ("rejected", "bad idea")


class TestCrossBatchApproval:
    @pytest.mark.asyncio
    async def test_resolve_all_pending_across_batches(self, db):
        """resolve_all_pending_proposals resolves proposals in multiple batches."""
        # Create two batches
        await ego_crud.create_proposal(
            db,
            id="b1p1",
            action_type="dispatch",
            content="Batch 1 proposal 1",
            batch_id="batch_a",
        )
        await ego_crud.create_proposal(
            db,
            id="b2p1",
            action_type="investigate",
            content="Batch 2 proposal 1",
            batch_id="batch_b",
        )

        workflow = ProposalWorkflow(db=db)
        results = await workflow.resolve_all_pending_proposals("approved")

        assert len(results) == 2
        assert results["b1p1"] == "approved"
        assert results["b2p1"] == "approved"

        # Verify in DB
        p1 = await ego_crud.get_proposal(db, "b1p1")
        p2 = await ego_crud.get_proposal(db, "b2p1")
        assert p1["status"] == "approved"
        assert p2["status"] == "approved"

    @pytest.mark.asyncio
    async def test_resolve_all_with_partially_resolved_batch(self, db):
        """Cross-batch approval works when a batch has some already-resolved proposals."""
        # Create batch with 3 proposals, resolve #1 first
        await ego_crud.create_proposal(
            db,
            id="mix_a1",
            action_type="dispatch",
            content="Already resolved",
            batch_id="mixed_batch",
        )
        await ego_crud.create_proposal(
            db,
            id="mix_a2",
            action_type="investigate",
            content="Still pending 1",
            batch_id="mixed_batch",
        )
        await ego_crud.create_proposal(
            db,
            id="mix_a3",
            action_type="dispatch",
            content="Still pending 2",
            batch_id="mixed_batch",
        )
        # Resolve the first one manually
        await ego_crud.resolve_proposal(db, "mix_a1", status="rejected")

        workflow = ProposalWorkflow(db=db)
        results = await workflow.resolve_all_pending_proposals("approved")

        # Only the 2 pending proposals should be resolved
        assert len(results) == 2
        assert "mix_a2" in results
        assert "mix_a3" in results
        assert "mix_a1" not in results  # was already rejected

        # Verify DB state
        p1 = await ego_crud.get_proposal(db, "mix_a1")
        p2 = await ego_crud.get_proposal(db, "mix_a2")
        p3 = await ego_crud.get_proposal(db, "mix_a3")
        assert p1["status"] == "rejected"  # unchanged
        assert p2["status"] == "approved"
        assert p3["status"] == "approved"

    @pytest.mark.asyncio
    async def test_resolve_all_empty(self, db):
        """resolve_all_pending_proposals returns empty dict when no pending."""
        workflow = ProposalWorkflow(db=db)
        results = await workflow.resolve_all_pending_proposals("approved")
        assert results == {}


class TestCancelRevoke:
    def test_cancel_all_parser(self):
        """'cancel all' returns sentinel 0 with 'cancelled' status."""
        result = parse_proposal_decisions("cancel all")
        assert 0 in result
        assert result[0] == ("cancelled", None)

    def test_cancel_numbered(self):
        """'cancel 1' returns numbered entry with 'cancelled' status."""
        result = parse_proposal_decisions("cancel 1")
        assert result[1] == ("cancelled", None)

    def test_revoke_word(self):
        """'revoke 2' works as cancel."""
        result = parse_proposal_decisions("revoke 2")
        assert result[2] == ("cancelled", None)

    @pytest.mark.asyncio
    async def test_revoke_approved_proposal(self, db):
        """revoke_proposal transitions approved -> rejected."""
        await ego_crud.create_proposal(
            db,
            id="rev1",
            action_type="dispatch",
            content="Will be revoked",
        )
        await ego_crud.resolve_proposal(db, "rev1", status="approved")
        ok = await ego_crud.revoke_proposal(db, "rev1")
        assert ok
        p = await ego_crud.get_proposal(db, "rev1")
        assert p["status"] == "rejected"
        assert p["user_response"] == "revoked by user"

    @pytest.mark.asyncio
    async def test_revoke_only_works_on_approved(self, db):
        """revoke_proposal does nothing on pending proposals."""
        await ego_crud.create_proposal(
            db,
            id="rev2",
            action_type="investigate",
            content="Still pending",
        )
        ok = await ego_crud.revoke_proposal(db, "rev2")
        assert not ok
        p = await ego_crud.get_proposal(db, "rev2")
        assert p["status"] == "pending"

    @pytest.mark.asyncio
    async def test_revoke_via_workflow(self, db):
        """ProposalWorkflow.revoke_approved_proposals revokes correct proposals."""
        await ego_crud.create_proposal(
            db,
            id="rw1",
            action_type="dispatch",
            content="Approved proposal",
            batch_id="rev_batch",
        )
        await ego_crud.create_proposal(
            db,
            id="rw2",
            action_type="investigate",
            content="Pending proposal",
            batch_id="rev_batch",
        )
        await ego_crud.resolve_proposal(db, "rw1", status="approved")

        workflow = ProposalWorkflow(db=db)
        revoked = await workflow.revoke_approved_proposals("rev_batch")
        assert revoked == 1

        p1 = await ego_crud.get_proposal(db, "rw1")
        p2 = await ego_crud.get_proposal(db, "rw2")
        assert p1["status"] == "rejected"
        assert p2["status"] == "pending"  # unchanged


class TestPendingCountHeader:
    @pytest.mark.asyncio
    async def test_send_digest_shows_pending_count(self, db):
        """send_digest includes pending count from other batches."""
        # Create pending proposals in another batch
        await ego_crud.create_proposal(
            db,
            id="old1",
            action_type="investigate",
            content="Old pending",
            batch_id="old_batch",
        )
        # Create the batch we're sending
        await ego_crud.create_proposal(
            db,
            id="new1",
            action_type="dispatch",
            content="New proposal",
            batch_id="new_batch",
        )

        mock_tm = AsyncMock()
        mock_tm.send_to_category = AsyncMock(return_value="delivery123")

        workflow = ProposalWorkflow(db=db, topic_manager=mock_tm)
        delivery_id = await workflow.send_digest("new_batch")

        assert delivery_id is not None
        # Check that the sent HTML contains the pending count
        sent_html = mock_tm.send_to_category.call_args[0][1]
        assert "1 proposal(s) pending from previous batches" in sent_html
        assert "approve all pending" in sent_html


# -- Layer 5: Execution tests --


class TestSweepLock:
    @pytest.mark.asyncio
    async def test_sweep_serialized(self):
        """Concurrent sweep calls are serialized by the lock."""
        from genesis.ego.session import EgoSession

        # Create a minimal EgoSession mock with the lock
        session = object.__new__(EgoSession)
        session._sweep_lock = asyncio.Lock()
        session._direct_session_runner = None

        # Both calls should complete (runner is None → early return)
        results = await asyncio.gather(
            session.sweep_approved_proposals(),
            session.sweep_approved_proposals(),
        )
        assert results == [[], []]


class TestBuildDispatchPrompt:
    @pytest.mark.asyncio
    async def test_basic_prompt(self, db):
        """Dispatch prompt includes proposal content, plan, and rationale."""
        from genesis.ego.session import EgoSession

        session = object.__new__(EgoSession)
        session._db = db

        prop = {
            "content": "Research AGI safety standards",
            "execution_plan": "Step 1: Search literature",
            "rationale": "User interested in safety",
        }
        prompt = await session._build_dispatch_prompt(prop)
        assert "Research AGI safety standards" in prompt
        assert "Step 1: Search literature" in prompt
        assert "User interested in safety" in prompt

    @pytest.mark.asyncio
    async def test_prompt_includes_goals(self, db):
        """Dispatch prompt includes active goals when available."""
        from genesis.db.crud import user_goals
        from genesis.ego.session import EgoSession

        await user_goals.create(
            db,
            title="Build thought leadership",
            category="career",
            priority="high",
        )

        session = object.__new__(EgoSession)
        session._db = db

        prop = {"content": "Test proposal", "execution_plan": None, "rationale": ""}
        prompt = await session._build_dispatch_prompt(prop)
        assert "Build thought leadership" in prompt
        assert "active goals" in prompt.lower()

    @pytest.mark.asyncio
    async def test_prompt_degrades_gracefully(self, db):
        """Dispatch prompt works even when world model tables are empty."""
        from genesis.ego.session import EgoSession

        session = object.__new__(EgoSession)
        session._db = db

        prop = {"content": "Basic proposal"}
        prompt = await session._build_dispatch_prompt(prop)
        assert "Basic proposal" in prompt
        # Should not crash even with no goals/contacts/events
