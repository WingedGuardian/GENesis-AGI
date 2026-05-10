"""Tests for intervention_journal CRUD operations."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import intervention_journal as journal_crud


@pytest.fixture
async def db(tmp_path):
    """In-memory DB with intervention_journal table."""
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""
            CREATE TABLE intervention_journal (
                id              TEXT PRIMARY KEY,
                ego_source      TEXT NOT NULL,
                proposal_id     TEXT,
                cycle_id        TEXT,
                action_type     TEXT NOT NULL,
                action_summary  TEXT NOT NULL,
                expected_outcome TEXT NOT NULL DEFAULT '',
                actual_outcome  TEXT,
                outcome_status  TEXT NOT NULL DEFAULT 'pending',
                user_response   TEXT,
                confidence      REAL DEFAULT 0.0,
                created_at      TEXT NOT NULL,
                resolved_at     TEXT
            )
        """)
        await conn.commit()
        yield conn


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_id(self, db):
        jid = await journal_crud.create(
            db,
            ego_source="user_ego_cycle",
            proposal_id="prop-1",
            cycle_id="cycle-1",
            action_type="investigate",
            action_summary="Look into memory usage",
            expected_outcome="Identify memory leaks",
            confidence=0.8,
        )
        assert isinstance(jid, str)
        assert len(jid) == 16

    @pytest.mark.asyncio
    async def test_create_stores_fields(self, db):
        await journal_crud.create(
            db,
            ego_source="genesis_ego_cycle",
            proposal_id="prop-2",
            cycle_id="cycle-2",
            action_type="outreach",
            action_summary="Send morning report",
            expected_outcome="User sees daily update",
            confidence=0.9,
        )
        entry = await journal_crud.get_by_proposal(db, "prop-2")
        assert entry is not None
        assert entry["ego_source"] == "genesis_ego_cycle"
        assert entry["action_type"] == "outreach"
        assert entry["outcome_status"] == "pending"
        assert entry["confidence"] == 0.9


class TestResolve:
    @pytest.mark.asyncio
    async def test_resolve_updates_pending(self, db):
        await journal_crud.create(
            db,
            ego_source="user_ego_cycle",
            proposal_id="prop-3",
            cycle_id="cycle-3",
            action_type="investigate",
            action_summary="Check logs",
        )
        ok = await journal_crud.resolve(
            db, "prop-3",
            outcome_status="approved",
            actual_outcome="User approved investigation",
            user_response="yes, go ahead",
        )
        assert ok is True

        entry = await journal_crud.get_by_proposal(db, "prop-3")
        assert entry["outcome_status"] == "approved"
        assert entry["actual_outcome"] == "User approved investigation"
        assert entry["user_response"] == "yes, go ahead"
        assert entry["resolved_at"] is not None

    @pytest.mark.asyncio
    async def test_resolve_returns_false_for_unknown_proposal(self, db):
        ok = await journal_crud.resolve(
            db, "nonexistent",
            outcome_status="approved",
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_resolve_only_updates_pending(self, db):
        await journal_crud.create(
            db,
            ego_source="user_ego_cycle",
            proposal_id="prop-4",
            cycle_id="cycle-4",
            action_type="investigate",
            action_summary="Check memory",
        )
        # Resolve once
        await journal_crud.resolve(db, "prop-4", outcome_status="approved")
        # Try to resolve again — should return False
        ok = await journal_crud.resolve(db, "prop-4", outcome_status="rejected")
        assert ok is False
        # Status should still be approved
        entry = await journal_crud.get_by_proposal(db, "prop-4")
        assert entry["outcome_status"] == "approved"


class TestQueries:
    @pytest.mark.asyncio
    async def test_recent_resolved(self, db):
        # Create and resolve 2 entries
        await journal_crud.create(
            db, ego_source="user_ego_cycle", proposal_id="p1",
            cycle_id="c1", action_type="investigate",
            action_summary="Check A",
        )
        await journal_crud.create(
            db, ego_source="user_ego_cycle", proposal_id="p2",
            cycle_id="c2", action_type="outreach",
            action_summary="Send B",
        )
        await journal_crud.resolve(db, "p1", outcome_status="approved")
        await journal_crud.resolve(db, "p2", outcome_status="rejected")

        resolved = await journal_crud.recent_resolved(db, days=7, limit=10)
        assert len(resolved) == 2
        # Newest first
        assert resolved[0]["action_type"] in ("investigate", "outreach")

    @pytest.mark.asyncio
    async def test_recent_resolved_excludes_pending(self, db):
        await journal_crud.create(
            db, ego_source="user_ego_cycle", proposal_id="p3",
            cycle_id="c3", action_type="investigate",
            action_summary="Check C",
        )
        # Don't resolve — should not appear
        resolved = await journal_crud.recent_resolved(db)
        assert len(resolved) == 0

    @pytest.mark.asyncio
    async def test_unresolved_count(self, db):
        await journal_crud.create(
            db, ego_source="user_ego_cycle", proposal_id="p4",
            cycle_id="c4", action_type="investigate",
            action_summary="Check D",
        )
        await journal_crud.create(
            db, ego_source="user_ego_cycle", proposal_id="p5",
            cycle_id="c5", action_type="investigate",
            action_summary="Check E",
        )
        assert await journal_crud.unresolved_count(db) == 2

        await journal_crud.resolve(db, "p4", outcome_status="approved")
        assert await journal_crud.unresolved_count(db) == 1

    @pytest.mark.asyncio
    async def test_aggregate_by_type(self, db):
        # Create several entries of different types
        for i, (atype, status) in enumerate([
            ("investigate", "approved"),
            ("investigate", "rejected"),
            ("investigate", "executed"),
            ("outreach", "approved"),
            ("outreach", "approved"),
        ]):
            await journal_crud.create(
                db, ego_source="user_ego_cycle",
                proposal_id=f"agg-{i}", cycle_id=f"c-{i}",
                action_type=atype, action_summary=f"Test {i}",
                confidence=0.7,
            )
            await journal_crud.resolve(
                db, f"agg-{i}", outcome_status=status,
            )

        aggs = await journal_crud.aggregate_by_type(db)
        assert len(aggs) == 2

        investigate = next(a for a in aggs if a["action_type"] == "investigate")
        assert investigate["total"] == 3
        assert investigate["approved"] == 1
        assert investigate["rejected"] == 1
        assert investigate["executed"] == 1

        outreach = next(a for a in aggs if a["action_type"] == "outreach")
        assert outreach["total"] == 2
        assert outreach["approved"] == 2
