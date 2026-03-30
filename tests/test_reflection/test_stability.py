"""Tests for genesis.reflection.stability — learning stability monitoring."""

import json
from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.db.crud import procedural
from genesis.db.schema import create_all_tables, seed_data
from genesis.reflection.stability import LearningStabilityMonitor


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


@pytest.fixture
def monitor():
    return LearningStabilityMonitor()


async def _insert_procedure(db, id: str, *, success: int = 5, failure: int = 1):
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO procedural_memory "
        "(id, task_type, principle, steps, tools_used, context_tags, "
        "success_count, failure_count, confidence, created_at) "
        "VALUES (?, 'test', 'principle', '[]', '[]', '[]', ?, ?, 0.5, ?)",
        (id, success, failure, now),
    )
    await db.commit()


class TestQuarantineCandidates:
    @pytest.mark.asyncio
    async def test_no_procedures(self, db, monitor):
        candidates = await monitor.check_quarantine_candidates(db)
        assert candidates == []

    @pytest.mark.asyncio
    async def test_healthy_procedure(self, db, monitor):
        await _insert_procedure(db, "p1", success=8, failure=2)
        candidates = await monitor.check_quarantine_candidates(db)
        assert candidates == []

    @pytest.mark.asyncio
    async def test_low_usage_not_quarantined(self, db, monitor):
        """< 3 uses → not enough data to quarantine."""
        await _insert_procedure(db, "p1", success=0, failure=2)
        candidates = await monitor.check_quarantine_candidates(db)
        assert candidates == []

    @pytest.mark.asyncio
    async def test_exactly_three_uses_below_threshold(self, db, monitor):
        """3 uses, 1/3 = 33% < 40% → candidate."""
        await _insert_procedure(db, "p1", success=1, failure=2)
        candidates = await monitor.check_quarantine_candidates(db)
        assert len(candidates) == 1
        assert candidates[0]["procedure_id"] == "p1"
        assert candidates[0]["success_rate"] < 0.40

    @pytest.mark.asyncio
    async def test_exactly_at_threshold(self, db, monitor):
        """40% exactly → not a candidate (< not <=)."""
        await _insert_procedure(db, "p1", success=2, failure=3)
        candidates = await monitor.check_quarantine_candidates(db)
        assert candidates == []

    @pytest.mark.asyncio
    async def test_multiple_candidates(self, db, monitor):
        await _insert_procedure(db, "p1", success=1, failure=4)
        await _insert_procedure(db, "p2", success=0, failure=5)
        await _insert_procedure(db, "p3", success=8, failure=2)  # healthy
        candidates = await monitor.check_quarantine_candidates(db)
        assert len(candidates) == 2


class TestExecuteQuarantine:
    @pytest.mark.asyncio
    async def test_quarantine_success(self, db, monitor):
        await _insert_procedure(db, "p1")
        result = await monitor.execute_quarantine(db, "p1", "test reason")
        assert result

        # Verify quarantined
        row = await procedural.get_by_id(db, "p1")
        assert row["quarantined"] == 1

        # Verify observation created
        cursor = await db.execute(
            "SELECT * FROM observations WHERE type = 'procedure_quarantined'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        content = json.loads(dict(rows[0])["content"])
        assert content["procedure_id"] == "p1"

    @pytest.mark.asyncio
    async def test_quarantine_nonexistent(self, db, monitor):
        result = await monitor.execute_quarantine(db, "nonexistent", "reason")
        assert not result

    @pytest.mark.asyncio
    async def test_quarantine_with_event_bus(self, db):
        from unittest.mock import AsyncMock
        bus = AsyncMock()
        monitor = LearningStabilityMonitor(event_bus=bus)
        await _insert_procedure(db, "p1")
        await monitor.execute_quarantine(db, "p1", "failing")
        bus.emit.assert_called_once()


class TestCheckRegression:
    @pytest.mark.asyncio
    async def test_no_assessments(self, db, monitor):
        result = await monitor.check_regression(db)
        assert not result

    @pytest.mark.asyncio
    async def test_insufficient_data(self, db, monitor):
        """Only 1 assessment → can't detect regression."""
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES ('a1', 'weekly_assessment', 'self_assessment', ?, 'medium', ?)",
            (json.dumps({"dimensions": [{"dimension": "procedure_effectiveness", "score": 0.5}]}), now),
        )
        await db.commit()
        result = await monitor.check_regression(db)
        assert not result

    @pytest.mark.asyncio
    async def test_declining_two_weeks(self, db, monitor):
        """Scores: 0.4 (newest), 0.6, 0.8 (oldest) → declining."""
        for i, score in enumerate([0.4, 0.6, 0.8]):
            await db.execute(
                "INSERT INTO observations (id, source, type, content, priority, created_at) "
                "VALUES (?, 'weekly_assessment', 'self_assessment', ?, 'medium', ?)",
                (
                    f"a{i}",
                    json.dumps({"dimensions": [{"dimension": "procedure_effectiveness", "score": score}]}),
                    f"2026-03-{10-i:02d}T00:00:00",  # newer first
                ),
            )
        await db.commit()
        result = await monitor.check_regression(db)
        assert result

    @pytest.mark.asyncio
    async def test_improving_not_regression(self, db, monitor):
        """Scores: 0.8, 0.6, 0.4 → improving, not regression."""
        for i, score in enumerate([0.8, 0.6, 0.4]):
            await db.execute(
                "INSERT INTO observations (id, source, type, content, priority, created_at) "
                "VALUES (?, 'weekly_assessment', 'self_assessment', ?, 'medium', ?)",
                (
                    f"a{i}",
                    json.dumps({"dimensions": [{"dimension": "procedure_effectiveness", "score": score}]}),
                    f"2026-03-{10-i:02d}T00:00:00",
                ),
            )
        await db.commit()
        result = await monitor.check_regression(db)
        assert not result

    @pytest.mark.asyncio
    async def test_one_week_decline_not_enough(self, db, monitor):
        """Only 1 decline, not 2 consecutive → no regression."""
        for i, score in enumerate([0.4, 0.6, 0.5]):
            await db.execute(
                "INSERT INTO observations (id, source, type, content, priority, created_at) "
                "VALUES (?, 'weekly_assessment', 'self_assessment', ?, 'medium', ?)",
                (
                    f"a{i}",
                    json.dumps({"dimensions": [{"dimension": "procedure_effectiveness", "score": score}]}),
                    f"2026-03-{10-i:02d}T00:00:00",
                ),
            )
        await db.commit()
        result = await monitor.check_regression(db)
        assert not result


class TestEmitRegressionSignal:
    @pytest.mark.asyncio
    async def test_writes_observation(self, db, monitor):
        await monitor.emit_regression_signal(db)

        cursor = await db.execute(
            "SELECT * FROM observations WHERE type = 'learning_regression'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert "LEARNING REGRESSION" in dict(row)["content"]

    @pytest.mark.asyncio
    async def test_emits_event(self, db):
        from unittest.mock import AsyncMock
        bus = AsyncMock()
        monitor = LearningStabilityMonitor(event_bus=bus)
        await monitor.emit_regression_signal(db)
        bus.emit.assert_called_once()


class TestFindContradictions:
    def test_no_contradictions(self, monitor):
        obs = [
            {"id": "a", "content": "The system should improve performance"},
            {"id": "b", "content": "The process works well"},
        ]
        result = monitor.find_contradictions(obs)
        assert result == []

    def test_simple_contradiction(self, monitor):
        obs = [
            {"id": "a", "content": "The deploy process should always include testing"},
            {"id": "b", "content": "The deploy process should never include testing"},
        ]
        result = monitor.find_contradictions(obs)
        assert len(result) == 1
        assert result[0][0] == "a"
        assert result[0][1] == "b"

    def test_no_shared_topic(self, monitor):
        """Negation pair but no shared topic words → not a contradiction."""
        obs = [
            {"id": "a", "content": "always increase the budget for marketing"},
            {"id": "b", "content": "never decrease the temperature for cooking"},
        ]
        result = monitor.find_contradictions(obs)
        assert result == []

    def test_empty_list(self, monitor):
        assert monitor.find_contradictions([]) == []

    def test_single_observation(self, monitor):
        assert monitor.find_contradictions([{"id": "a", "content": "test"}]) == []


class TestMatcherExcludesQuarantined:
    @pytest.mark.asyncio
    async def test_quarantined_excluded(self, db):
        from genesis.learning.procedural.matcher import find_best_match
        now = datetime.now(UTC).isoformat()

        # Insert a quarantined procedure
        await db.execute(
            "INSERT INTO procedural_memory "
            "(id, task_type, principle, steps, tools_used, context_tags, "
            "success_count, failure_count, confidence, quarantined, created_at) "
            "VALUES ('p1', 'deploy', 'fast deploy', '[]', '[]', '[\"ci\"]', "
            "10, 0, 0.9, 1, ?)",
            (now,),
        )
        await db.commit()

        result = await find_best_match(db, "deploy", ["ci"])
        assert result is None  # Quarantined, so no match

    @pytest.mark.asyncio
    async def test_non_quarantined_matches(self, db):
        from genesis.learning.procedural.matcher import find_best_match
        now = datetime.now(UTC).isoformat()

        await db.execute(
            "INSERT INTO procedural_memory "
            "(id, task_type, principle, steps, tools_used, context_tags, "
            "success_count, failure_count, confidence, quarantined, created_at) "
            "VALUES ('p1', 'deploy', 'fast deploy', '[]', '[]', '[\"ci\"]', "
            "10, 0, 0.9, 0, ?)",
            (now,),
        )
        await db.commit()

        result = await find_best_match(db, "deploy", ["ci"])
        assert result is not None
        assert result.procedure_id == "p1"
