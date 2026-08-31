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


class TestProposalDeduplication:
    """`exclude_proposals_within_days` shipped untested; these cover it.

    A proposal batch writes BOTH an ego_proposals row and a matching
    intervention_journal row, and the capability aggregator reads both sources
    under the same action type -- so two observations counted as four and
    cleared a three-sample floor built to suppress exactly that.
    """

    @staticmethod
    async def _with_proposals(db):
        await db.execute("""
            CREATE TABLE ego_proposals (
                id          TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL,
                status      TEXT NOT NULL
            )
        """)
        await db.commit()

    @staticmethod
    async def _journal_row(db, jid, proposal_id):
        await db.execute(
            "INSERT INTO intervention_journal (id, ego_source, proposal_id, "
            "action_type, action_summary, outcome_status, confidence, created_at) "
            "VALUES (?, 'genesis', ?, 'dispatch', 's', 'approved', 0.5, "
            "datetime('now'))",
            (jid, proposal_id),
        )
        await db.commit()

    @pytest.mark.asyncio
    async def test_a_duplicated_proposal_is_counted_once(self, db):
        await self._with_proposals(db)
        await self._journal_row(db, "j1", "p1")
        await db.execute(
            "INSERT INTO ego_proposals (id, created_at, status) "
            "VALUES ('p1', datetime('now'), 'approved')"
        )
        await db.commit()
        rows = await journal_crud.aggregate_by_type(
            db, exclude_proposals_within_days=30
        )
        assert not rows, f"the journal copy of a recent proposal was counted: {rows}"

    @pytest.mark.asyncio
    async def test_a_null_proposal_id_does_not_discard_the_history(self, db):
        """One NULL id must not wipe every unrelated row.

        `x NOT IN (…)` is NULL -- never true -- the moment the subquery yields a
        single NULL, so the whole resolved history would be discarded rather
        than the duplicates. `id` is a TEXT PRIMARY KEY, which SQLite does not
        constrain to NOT NULL, so this is reachable in an imported or repaired
        database. Silent, and it would shrink every domain's sample size.
        """
        await self._with_proposals(db)
        await self._journal_row(db, "j1", "unrelated-proposal")
        await db.execute(
            "INSERT INTO ego_proposals (id, created_at, status) "
            "VALUES (NULL, datetime('now'), 'approved')"
        )
        await db.commit()
        rows = await journal_crud.aggregate_by_type(
            db, exclude_proposals_within_days=30
        )
        assert rows, (
            "a single NULL proposal id discarded the entire resolved history"
        )
        assert rows[0]["total"] == 1


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


class TestNegativeWindowIsRefused:
    """A negative window must RAISE, not silently disable the de-duplication.

    Measured, not reasoned: SQLite renders a negative value as the modifier
    ``'--N days'``, which it rejects, yielding NULL. ``p.created_at >= NULL``
    is then NULL rather than false, so ``NOT EXISTS`` holds for every row and
    the exclusion becomes a no-op -- every proposal counted twice again, from a
    call that returns a perfectly healthy-looking result set. That is the worst
    shape a validation gap can take: a wrong answer wearing a right answer's
    grammar.

    ``capability_map._recency_clause`` already refuses this loudly. The two
    windowed APIs disagreeing about the same input is how one of them gets
    "fixed" later by copying the wrong one.
    """

    @staticmethod
    async def _with_proposals(db):
        """The exclusion SQL joins ego_proposals; the base fixture omits it.

        Created here so the accepted-values case exercises the REAL query
        instead of dying on a missing table -- a test that fails for a setup
        reason proves nothing about the guard it is named for.
        """
        await db.execute(
            "CREATE TABLE ego_proposals (id TEXT PRIMARY KEY, "
            "created_at TEXT NOT NULL, status TEXT NOT NULL)"
        )
        await db.commit()

    @pytest.mark.asyncio
    async def test_negative_window_raises(self, db):
        await self._with_proposals(db)
        with pytest.raises(ValueError, match="must be >= 0 or None"):
            await journal_crud.aggregate_by_type(
                db, exclude_proposals_within_days=-5,
            )

    @pytest.mark.asyncio
    async def test_zero_and_none_are_still_accepted(self, db):
        """0 is a legitimate empty window; None disables the exclusion.

        Asserted against a table with a ROW in it. `== []` on an empty table is
        equally true for a correct window, a broken one, and a function that
        returns [] unconditionally -- the assertion would hold on the success
        path either way, which makes it decoration rather than a test.
        """
        await self._with_proposals(db)
        await journal_crud.create(
            db, ego_source="genesis", proposal_id="p-z", cycle_id="c-z",
            action_type="dispatch", action_summary="s", confidence=0.5,
        )
        await journal_crud.resolve(db, "p-z", outcome_status="approved")

        for window in (0, None):
            aggs = await journal_crud.aggregate_by_type(
                db, exclude_proposals_within_days=window,
            )
            assert [a["action_type"] for a in aggs] == ["dispatch"], (
                f"window={window!r} dropped a row it should have counted"
            )
            assert aggs[0]["total"] == 1
