"""Tests for the capability aggregator."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.ego.capability_aggregator import compute_capability_map


@pytest.fixture
async def db(tmp_path):
    """DB with minimal tables for aggregation testing."""
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        # intervention_journal
        await conn.execute("""
            CREATE TABLE intervention_journal (
                id TEXT PRIMARY KEY, ego_source TEXT, proposal_id TEXT,
                cycle_id TEXT, action_type TEXT NOT NULL,
                action_summary TEXT NOT NULL, expected_outcome TEXT DEFAULT '',
                actual_outcome TEXT, outcome_status TEXT DEFAULT 'pending',
                user_response TEXT, confidence REAL DEFAULT 0.0,
                created_at TEXT NOT NULL, resolved_at TEXT
            )
        """)
        # ego_proposals
        await conn.execute("""
            CREATE TABLE ego_proposals (
                id TEXT PRIMARY KEY, action_type TEXT NOT NULL,
                action_category TEXT DEFAULT '', content TEXT NOT NULL,
                rationale TEXT DEFAULT '', confidence REAL DEFAULT 0.0,
                urgency TEXT DEFAULT 'normal',
                status TEXT DEFAULT 'pending',
                user_response TEXT, cycle_id TEXT, batch_id TEXT,
                created_at TEXT NOT NULL, resolved_at TEXT,
                expires_at TEXT, rank INTEGER,
                execution_plan TEXT, recurring INTEGER DEFAULT 0,
                memory_basis TEXT DEFAULT ''
            )
        """)
        # autonomy_state
        await conn.execute("""
            CREATE TABLE autonomy_state (
                id TEXT PRIMARY KEY, person_id TEXT,
                category TEXT NOT NULL, current_level INTEGER DEFAULT 1,
                earned_level INTEGER DEFAULT 1,
                context_ceiling INTEGER,
                consecutive_corrections INTEGER DEFAULT 0,
                total_successes INTEGER DEFAULT 0,
                total_corrections INTEGER DEFAULT 0,
                last_correction_at TEXT, last_regression_at TEXT,
                regression_reason TEXT, updated_at TEXT
            )
        """)
        # procedural_memory
        await conn.execute("""
            CREATE TABLE procedural_memory (
                id TEXT PRIMARY KEY, task_type TEXT NOT NULL,
                confidence REAL DEFAULT 0.0, deprecated INTEGER DEFAULT 0,
                quarantined INTEGER DEFAULT 0, speculative INTEGER DEFAULT 1,
                success_count INTEGER DEFAULT 0, failure_count INTEGER DEFAULT 0,
                invocation_count INTEGER DEFAULT 0,
                activation_tier TEXT DEFAULT 'L4',
                tool_trigger TEXT,
                created_at TEXT, updated_at TEXT
            )
        """)
        # cc_sessions
        await conn.execute("""
            CREATE TABLE cc_sessions (
                id TEXT PRIMARY KEY, session_type TEXT NOT NULL,
                model TEXT NOT NULL, effort TEXT DEFAULT 'medium',
                status TEXT, source_tag TEXT DEFAULT 'foreground',
                cost_usd REAL, started_at TEXT NOT NULL,
                completed_at TEXT, metadata TEXT
            )
        """)
        await conn.commit()
        yield conn


class TestComputeCapabilityMap:
    @pytest.mark.asyncio
    async def test_empty_tables_returns_empty(self, db):
        results = await compute_capability_map(db)
        assert results == []

    @pytest.mark.asyncio
    async def test_journal_data_only(self, db):
        """Intervention journal data produces capability entries."""
        from datetime import UTC, datetime
        now = datetime.now(UTC).isoformat()
        # 3 investigate proposals: 2 approved, 1 rejected
        for i, status in enumerate(["approved", "approved", "rejected"]):
            await db.execute(
                "INSERT INTO intervention_journal "
                "(id, ego_source, proposal_id, cycle_id, action_type, "
                "action_summary, outcome_status, confidence, created_at, resolved_at) "
                "VALUES (?, 'user_ego', ?, ?, 'investigate', 'test', ?, 0.8, ?, ?)",
                (f"j{i}", f"p{i}", f"c{i}", status, now, now),
            )
        await db.commit()

        results = await compute_capability_map(db)
        assert len(results) >= 1
        investigate = next((r for r in results if r["domain"] == "investigate"), None)
        assert investigate is not None
        # 2/3 approved → ~67%
        assert 0.6 <= investigate["confidence"] <= 0.7

    @pytest.mark.asyncio
    async def test_multiple_sources_weighted(self, db):
        """Multiple data sources for same domain are weighted by sample size."""
        from datetime import UTC, datetime
        now = datetime.now(UTC).isoformat()

        # Journal: 1 approved out of 1 → 100% but small sample
        await db.execute(
            "INSERT INTO intervention_journal "
            "(id, ego_source, action_type, action_summary, outcome_status, "
            "confidence, created_at, resolved_at) "
            "VALUES ('j1', 'user_ego', 'investigate', 'test', 'approved', 0.8, ?, ?)",
            (now, now),
        )
        # Proposals: 5 approved out of 10 → 50% with larger sample
        for i in range(10):
            status = "approved" if i < 5 else "rejected"
            await db.execute(
                "INSERT INTO ego_proposals "
                "(id, action_type, content, status, created_at) "
                "VALUES (?, 'investigate', 'test', ?, ?)",
                (f"p{i}", status, now),
            )
        await db.commit()

        results = await compute_capability_map(db)
        investigate = next((r for r in results if r["domain"] == "investigate"), None)
        assert investigate is not None
        # Weighted by sample size: proposals (n=10) dominate over journal (n=1)
        # So result should be closer to 50% than 100%
        assert 0.45 <= investigate["confidence"] <= 0.65

    @pytest.mark.asyncio
    async def test_autonomy_state_contributes(self, db):
        """Autonomy Bayesian posteriors contribute to the map."""
        await db.execute(
            "INSERT INTO autonomy_state "
            "(id, category, total_successes, total_corrections) "
            "VALUES ('a1', 'outreach', 8, 2)",
        )
        await db.commit()

        results = await compute_capability_map(db)
        outreach = next((r for r in results if r["domain"] == "outreach"), None)
        assert outreach is not None
        # Posterior = (8+1)/(8+2+2) = 9/12 = 0.75
        assert 0.7 <= outreach["confidence"] <= 0.8

    @pytest.mark.asyncio
    async def test_results_sorted_by_confidence(self, db):
        """Results come back sorted highest confidence first."""
        await db.execute(
            "INSERT INTO autonomy_state (id, category, total_successes, total_corrections) "
            "VALUES ('a1', 'low_domain', 1, 8)"
        )
        await db.execute(
            "INSERT INTO autonomy_state (id, category, total_successes, total_corrections) "
            "VALUES ('a2', 'high_domain', 9, 1)"
        )
        await db.commit()

        results = await compute_capability_map(db)
        assert len(results) == 2
        assert results[0]["domain"] == "high_domain"
        assert results[1]["domain"] == "low_domain"
