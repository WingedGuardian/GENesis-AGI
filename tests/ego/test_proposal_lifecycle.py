"""Tests for proposal lifecycle redesign — board/queue separation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.crud import intervention_journal as journal_crud

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(days_ago: int = 0, hours_ago: int = 0) -> str:
    """ISO timestamp *days_ago* days and *hours_ago* hours in the past."""
    return (
        datetime.now(UTC) - timedelta(days=days_ago, hours=hours_ago)
    ).isoformat()


async def _create(
    db, id: str, *, rank=None, created_at=None, status="pending", urgency="normal"
):
    """Shorthand to create a proposal with sensible defaults."""
    await ego_crud.create_proposal(
        db,
        id=id,
        action_type="investigate",
        content=f"Proposal {id}",
        rank=rank,
        created_at=created_at or _ts(),
        status=status,
        urgency=urgency,
    )


# ---------------------------------------------------------------------------
# unboard_proposal
# ---------------------------------------------------------------------------


class TestUnboardProposal:
    @pytest.mark.asyncio
    async def test_unboard_ranked_pending(self, db):
        """Unboarding clears rank but keeps status pending."""
        await _create(db, "p1", rank=1)
        ok = await ego_crud.unboard_proposal(db, "p1")
        assert ok is True
        row = await ego_crud.get_proposal(db, "p1")
        assert row["status"] == "pending"
        assert row["rank"] is None

    @pytest.mark.asyncio
    async def test_unboard_already_unranked(self, db):
        """Unboarding an already-unranked proposal succeeds (idempotent)."""
        await _create(db, "p1")  # no rank
        ok = await ego_crud.unboard_proposal(db, "p1")
        assert ok is True
        row = await ego_crud.get_proposal(db, "p1")
        assert row["status"] == "pending"
        assert row["rank"] is None

    @pytest.mark.asyncio
    async def test_unboard_nonpending_fails(self, db):
        """Cannot unboard a proposal that is not pending."""
        await _create(db, "p1", rank=1)
        await ego_crud.resolve_proposal(db, "p1", status="approved")
        ok = await ego_crud.unboard_proposal(db, "p1")
        assert ok is False

    @pytest.mark.asyncio
    async def test_unboard_nonexistent(self, db):
        """Unboarding a nonexistent proposal returns False."""
        ok = await ego_crud.unboard_proposal(db, "does-not-exist")
        assert ok is False


# ---------------------------------------------------------------------------
# get_pending_queue
# ---------------------------------------------------------------------------


class TestGetPendingQueue:
    @pytest.mark.asyncio
    async def test_returns_all_pending(self, db):
        """Queue returns every pending proposal regardless of rank."""
        for i in range(5):
            await _create(db, f"p{i}", created_at=_ts(days_ago=i))
        queue = await ego_crud.get_pending_queue(db)
        assert len(queue) == 5

    @pytest.mark.asyncio
    async def test_ranked_before_unranked(self, db):
        """Ranked proposals appear before unranked ones."""
        await _create(db, "unranked", created_at=_ts(hours_ago=1))
        await _create(db, "ranked", rank=1, created_at=_ts(hours_ago=2))
        queue = await ego_crud.get_pending_queue(db)
        assert queue[0]["id"] == "ranked"
        assert queue[1]["id"] == "unranked"

    @pytest.mark.asyncio
    async def test_rank_ordering(self, db):
        """Lower rank sorts first."""
        await _create(db, "rank2", rank=2, created_at=_ts())
        await _create(db, "rank1", rank=1, created_at=_ts())
        queue = await ego_crud.get_pending_queue(db)
        assert queue[0]["id"] == "rank1"
        assert queue[1]["id"] == "rank2"

    @pytest.mark.asyncio
    async def test_excludes_nonpending(self, db):
        """Non-pending proposals are excluded."""
        await _create(db, "p1")
        await _create(db, "p2")
        await ego_crud.resolve_proposal(db, "p2", status="approved")
        queue = await ego_crud.get_pending_queue(db)
        assert len(queue) == 1
        assert queue[0]["id"] == "p1"

    @pytest.mark.asyncio
    async def test_filter_by_ego_source(self, db):
        """ego_source filter restricts to matching proposals."""
        await ego_crud.create_proposal(
            db,
            id="user1",
            action_type="investigate",
            content="User ego proposal",
            ego_source="user_ego_cycle",
        )
        await ego_crud.create_proposal(
            db,
            id="gen1",
            action_type="maintenance",
            content="Genesis ego proposal",
            ego_source="genesis_ego_cycle",
        )
        queue = await ego_crud.get_pending_queue(db, ego_source="user_ego_cycle")
        ids = [p["id"] for p in queue]
        assert "user1" in ids
        assert "gen1" not in ids

    @pytest.mark.asyncio
    async def test_empty_queue(self, db):
        """Empty queue returns empty list."""
        queue = await ego_crud.get_pending_queue(db)
        assert queue == []


# ---------------------------------------------------------------------------
# auto_table_stale_proposals
# ---------------------------------------------------------------------------


class TestAutoTableStaleProposals:
    # Explicit windows keep these deterministic regardless of ambient config.
    _TTL = {"critical": 48, "high": 72, "normal": 120, "low": 168}

    @pytest.mark.asyncio
    async def test_tables_by_urgency_window(self, db):
        """Ranked proposals are tabled once older than their per-urgency window
        (high 3d, low 7d); fresher / within-window ones stay pending."""
        # ranked so the unranked floor does not mask the urgency window.
        await _create(db, "high_stale", rank=1, urgency="high", created_at=_ts(days_ago=4))
        await _create(db, "high_fresh", rank=2, urgency="high", created_at=_ts(days_ago=2))
        await _create(db, "low_within", rank=3, urgency="low", created_at=_ts(days_ago=6))
        count = await ego_crud.auto_table_stale_proposals(db, ttl_hours=self._TTL)
        assert count == 1
        assert (await ego_crud.get_proposal(db, "high_stale"))["status"] == "tabled"
        assert (await ego_crud.get_proposal(db, "high_stale"))["rank"] is None
        assert (await ego_crud.get_proposal(db, "high_fresh"))["status"] == "pending"
        assert (await ego_crud.get_proposal(db, "low_within"))["status"] == "pending"

    @pytest.mark.asyncio
    async def test_unranked_floor_ages_out_faster(self, db):
        """Unranked (never boarded) proposals table at the floor even when their
        urgency window is longer (low window 7d, floor 5d)."""
        await _create(db, "low_unranked", urgency="low", created_at=_ts(days_ago=6))
        await _create(db, "low_ranked", rank=1, urgency="low", created_at=_ts(days_ago=6))
        count = await ego_crud.auto_table_stale_proposals(
            db, ttl_hours=self._TTL, unranked_cap_hours=120
        )
        assert count == 1
        assert (await ego_crud.get_proposal(db, "low_unranked"))["status"] == "tabled"
        assert (await ego_crud.get_proposal(db, "low_ranked"))["status"] == "pending"

    @pytest.mark.asyncio
    async def test_only_affects_pending(self, db):
        """Auto-table does not touch non-pending proposals."""
        await _create(db, "old_approved", rank=1, created_at=_ts(days_ago=15))
        await ego_crud.resolve_proposal(db, "old_approved", status="approved")
        count = await ego_crud.auto_table_stale_proposals(db, ttl_hours=self._TTL)
        assert count == 0
        prop = await ego_crud.get_proposal(db, "old_approved")
        assert prop["status"] == "approved"

    @pytest.mark.asyncio
    async def test_updates_intervention_journal(self, db):
        """Auto-tabling also updates matching journal entries."""
        await _create(db, "old_j", rank=1, created_at=_ts(days_ago=15))
        await journal_crud.create(
            db,
            ego_source="user_ego_cycle",
            proposal_id="old_j",
            cycle_id="cycle1",
            action_type="investigate",
            action_summary="Journal test",
        )
        count = await ego_crud.auto_table_stale_proposals(db, ttl_hours=self._TTL)
        assert count == 1

        journal = await journal_crud.get_by_proposal(db, "old_j")
        assert journal is not None
        assert journal["outcome_status"] == "tabled"

    @pytest.mark.asyncio
    async def test_nothing_stale(self, db):
        """Returns 0 when no proposals exceed their window."""
        await _create(db, "fresh", rank=1, created_at=_ts(days_ago=1))
        count = await ego_crud.auto_table_stale_proposals(db, ttl_hours=self._TTL)
        assert count == 0

    @pytest.mark.asyncio
    async def test_defaults_to_config_when_ttl_omitted(self, db):
        """With no explicit ttl_hours the sweep loads config defaults (or the
        safe fallback) and still tables a clearly-stale proposal."""
        await _create(db, "ancient", rank=1, created_at=_ts(days_ago=30))
        count = await ego_crud.auto_table_stale_proposals(db)
        assert count == 1
        assert (await ego_crud.get_proposal(db, "ancient"))["status"] == "tabled"
