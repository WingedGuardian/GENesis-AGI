"""Tests for genesis.reflection.context_gatherer."""

import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables, seed_data
from genesis.reflection.context_gatherer import ContextGatherer


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


@pytest.fixture
def gatherer():
    return ContextGatherer(budget_daily=2.0, budget_weekly=10.0, budget_monthly=30.0)


class TestDetectPendingWork:
    @pytest.mark.asyncio
    async def test_no_pending_work_empty_db(self, db, gatherer):
        pending = await gatherer.detect_pending_work(db)
        assert not pending.memory_consolidation
        assert not pending.surplus_review
        assert pending.cost_reconciliation  # always true
        assert not pending.has_any_work or pending.cost_reconciliation

    @pytest.mark.asyncio
    async def test_memory_consolidation_below_threshold(self, db, gatherer):
        """Less than 10 unresolved obs → no memory consolidation."""
        now = datetime.now(UTC).isoformat()
        for _i in range(5):
            await db.execute(
                "INSERT INTO observations (id, source, type, content, priority, created_at) "
                "VALUES (?, 'test', 'test', 'content', 'low', ?)",
                (str(uuid.uuid4()), now),
            )
        await db.commit()
        pending = await gatherer.detect_pending_work(db)
        assert not pending.memory_consolidation
        assert pending.observation_backlog == 5

    @pytest.mark.asyncio
    async def test_memory_consolidation_above_threshold(self, db, gatherer):
        """10+ unresolved obs → memory consolidation needed."""
        now = datetime.now(UTC).isoformat()
        for _i in range(12):
            await db.execute(
                "INSERT INTO observations (id, source, type, content, priority, created_at) "
                "VALUES (?, 'test', 'test', 'content', 'low', ?)",
                (str(uuid.uuid4()), now),
            )
        await db.commit()
        pending = await gatherer.detect_pending_work(db)
        assert pending.memory_consolidation
        assert pending.observation_backlog == 12

    @pytest.mark.asyncio
    async def test_surplus_review_pending(self, db, gatherer):
        """Pending surplus items → surplus review needed."""
        now = datetime.now(UTC).isoformat()
        ttl = (datetime.now(UTC) + timedelta(days=3)).isoformat()
        await db.execute(
            "INSERT INTO surplus_insights "
            "(id, content, source_task_type, generating_model, drive_alignment, "
            "confidence, created_at, ttl) VALUES (?, 'test', 'brainstorm', "
            "'model', 'curiosity', 0.5, ?, ?)",
            (str(uuid.uuid4()), now, ttl),
        )
        await db.commit()
        pending = await gatherer.detect_pending_work(db)
        assert pending.surplus_review
        assert pending.surplus_pending == 1

    @pytest.mark.asyncio
    async def test_cognitive_state_stale(self, db, gatherer):
        """Cognitive state older than 24h → regeneration needed."""
        old_time = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        await db.execute(
            "INSERT INTO cognitive_state (id, content, section, generated_by, created_at) "
            "VALUES ('cog1', 'old state', 'active_context', 'test', ?)",
            (old_time,),
        )
        await db.commit()
        pending = await gatherer.detect_pending_work(db)
        assert pending.cognitive_regeneration

    @pytest.mark.asyncio
    async def test_cognitive_state_fresh(self, db, gatherer):
        """Recent cognitive state → no regeneration needed."""
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO cognitive_state (id, content, section, generated_by, created_at) "
            "VALUES ('cog1', 'fresh state', 'active_context', 'test', ?)",
            (now,),
        )
        await db.commit()
        pending = await gatherer.detect_pending_work(db)
        assert not pending.cognitive_regeneration

    @pytest.mark.asyncio
    async def test_lessons_extraction_with_observations(self, db, gatherer):
        """Any unresolved observations → lessons extraction possible."""
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES ('obs1', 'test', 'test', 'content', 'low', ?)",
            (now,),
        )
        await db.commit()
        pending = await gatherer.detect_pending_work(db)
        assert pending.lessons_extraction


class TestGather:
    @pytest.mark.asyncio
    async def test_gather_empty_db(self, db, gatherer):
        bundle = await gatherer.gather(db)
        assert bundle.recent_observations == []
        assert bundle.surplus_staging_items == []
        assert isinstance(bundle.cost_summary.daily_usd, float)

    @pytest.mark.asyncio
    async def test_gather_with_data(self, db, gatherer):
        now = datetime.now(UTC).isoformat()
        for _i in range(3):
            await db.execute(
                "INSERT INTO observations (id, source, type, content, priority, created_at) "
                "VALUES (?, 'test', 'test', 'content', 'low', ?)",
                (str(uuid.uuid4()), now),
            )
        await db.commit()
        bundle = await gatherer.gather(db)
        assert len(bundle.recent_observations) == 3

    @pytest.mark.asyncio
    async def test_gather_excludes_resolved_observations(self, db, gatherer):
        """Resolved observations must not appear in recent_observations (telephone game fix)."""
        now = datetime.now(UTC).isoformat()
        # Create 3 unresolved and 2 resolved observations
        for i in range(3):
            await db.execute(
                "INSERT INTO observations (id, source, type, content, priority, created_at, resolved) "
                "VALUES (?, 'test', 'test', 'unresolved content', 'low', ?, 0)",
                (f"unresolved-{i}", now),
            )
        for i in range(2):
            await db.execute(
                "INSERT INTO observations (id, source, type, content, priority, created_at, resolved) "
                "VALUES (?, 'test', 'test', 'resolved content', 'low', ?, 1)",
                (f"resolved-{i}", now),
            )
        await db.commit()
        bundle = await gatherer.gather(db)
        # Only unresolved observations should be returned
        assert len(bundle.recent_observations) == 3
        for obs in bundle.recent_observations:
            assert obs.get("resolved", 0) == 0


class TestGatherForAssessment:
    @pytest.mark.asyncio
    async def test_returns_six_dimensions(self, db, gatherer):
        data = await gatherer.gather_for_assessment(db)
        assert "reflection_quality" in data
        assert "procedure_effectiveness" in data
        assert "outreach_calibration" in data
        assert "learning_velocity" in data
        assert "resource_efficiency" in data
        assert "blind_spots" in data


class TestGatherForCalibration:
    @pytest.mark.asyncio
    async def test_returns_expected_keys(self, db, gatherer):
        data = await gatherer.gather_for_calibration(db)
        assert "procedure_stats" in data
        assert "cost_summary" in data
        assert "recent_assessments" in data


class TestProcedureStats:
    @pytest.mark.asyncio
    async def test_empty_procedures(self, db, gatherer):
        bundle = await gatherer.gather(db)
        assert bundle.procedure_stats.total_active == 0

    @pytest.mark.asyncio
    async def test_with_procedures(self, db, gatherer):
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO procedural_memory "
            "(id, task_type, principle, steps, tools_used, context_tags, "
            "success_count, failure_count, confidence, created_at) "
            "VALUES ('p1', 'test', 'do it', '[]', '[]', '[]', 8, 2, 0.8, ?)",
            (now,),
        )
        await db.commit()
        bundle = await gatherer.gather(db)
        assert bundle.procedure_stats.total_active == 1
        assert bundle.procedure_stats.avg_success_rate == 0.8

    @pytest.mark.asyncio
    async def test_low_performers_detected(self, db, gatherer):
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO procedural_memory "
            "(id, task_type, principle, steps, tools_used, context_tags, "
            "success_count, failure_count, confidence, created_at) "
            "VALUES ('p1', 'test', 'do it', '[]', '[]', '[]', 1, 4, 0.2, ?)",
            (now,),
        )
        await db.commit()
        bundle = await gatherer.gather(db)
        assert len(bundle.procedure_stats.low_performers) == 1
        assert bundle.procedure_stats.low_performers[0]["success_rate"] == 0.2


class TestCostSummary:
    @pytest.mark.asyncio
    async def test_empty_costs(self, db, gatherer):
        bundle = await gatherer.gather(db)
        assert bundle.cost_summary.daily_usd == 0.0

    @pytest.mark.asyncio
    async def test_with_cost_events(self, db, gatherer):
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO cost_events (id, event_type, cost_usd, created_at) "
            "VALUES ('c1', 'llm_call', 0.5, ?)",
            (now,),
        )
        await db.commit()
        bundle = await gatherer.gather(db)
        assert bundle.cost_summary.daily_usd == 0.5
        assert bundle.cost_summary.daily_budget_pct == 0.25
