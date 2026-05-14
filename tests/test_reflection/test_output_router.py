"""Tests for genesis.reflection.output_router — parsing and routing."""

import json

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables, seed_data
from genesis.reflection.output_router import (
    OutputRouter,
    parse_deep_reflection_output,
    parse_quality_calibration_output,
    parse_weekly_assessment_output,
)
from genesis.reflection.types import (
    DeepReflectionOutput,
    QualityCalibrationOutput,
    WeeklyAssessmentOutput,
)

# ── Parsing tests ─────────────────────────────────────────────────────


class TestParseDeepReflectionOutput:
    def test_valid_json(self):
        raw = json.dumps({
            "observations": ["obs1", "obs2"],
            "cognitive_state_update": "new state",
            "learnings": ["lesson1"],
            "confidence": 0.85,
            "focus_next": "watch X",
        })
        out = parse_deep_reflection_output(raw)
        assert len(out.observations) == 2
        assert out.cognitive_state_update == "new state"
        assert out.confidence == 0.85
        assert out.learnings == ["lesson1"]

    def test_json_in_code_block(self):
        raw = '```json\n{"observations": ["obs1"]}\n```'
        out = parse_deep_reflection_output(raw)
        assert out.observations == ["obs1"]

    def test_invalid_json(self):
        out = parse_deep_reflection_output("not json at all")
        assert isinstance(out, DeepReflectionOutput)
        assert out.observations == []

    def test_empty_string(self):
        out = parse_deep_reflection_output("")
        assert out.observations == []

    def test_none_input(self):
        out = parse_deep_reflection_output(None)  # type: ignore[arg-type]
        assert out.observations == []

    def test_non_dict_json(self):
        out = parse_deep_reflection_output('"just a string"')
        assert out.observations == []

    def test_memory_operations_parsed(self):
        raw = json.dumps({
            "memory_operations": [
                {"operation": "dedup", "target_ids": ["a", "b"], "reason": "similar"},
            ],
        })
        out = parse_deep_reflection_output(raw)
        assert len(out.memory_operations) == 1
        assert out.memory_operations[0].operation == "dedup"

    def test_surplus_decisions_ignored(self):
        """Old outputs with surplus_decisions should parse without error."""
        raw = json.dumps({
            "surplus_decisions": [
                {"item_id": "s1", "action": "promote", "reason": "good"},
            ],
        })
        out = parse_deep_reflection_output(raw)
        # surplus_decisions field no longer exists on output
        assert not hasattr(out, "surplus_decisions")

    def test_missing_fields_get_defaults(self):
        raw = json.dumps({"observations": ["only this"]})
        out = parse_deep_reflection_output(raw)
        assert out.observations == ["only this"]
        assert out.cognitive_state_update is None
        assert out.confidence == 0.7
        assert out.memory_operations == []


class TestParseWeeklyAssessmentOutput:
    def test_valid_output(self):
        raw = json.dumps({
            "dimensions": [
                {"dimension": "reflection_quality", "score": 0.8, "evidence": "good"},
            ],
            "overall_score": 0.75,
            "observations": ["doing well"],
        })
        out = parse_weekly_assessment_output(raw)
        assert len(out.dimensions) == 1
        assert out.overall_score == 0.75

    def test_invalid_json(self):
        out = parse_weekly_assessment_output("bad")
        assert isinstance(out, WeeklyAssessmentOutput)
        assert out.dimensions == []


class TestParseQualityCalibrationOutput:
    def test_drift_detected(self):
        raw = json.dumps({
            "drift_detected": True,
            "quarantine_candidates": ["p1"],
            "observations": ["quality declining"],
        })
        out = parse_quality_calibration_output(raw)
        assert out.drift_detected
        assert out.quarantine_candidates == ["p1"]

    def test_no_drift(self):
        raw = json.dumps({"drift_detected": False})
        out = parse_quality_calibration_output(raw)
        assert not out.drift_detected


# ── Routing tests ─────────────────────────────────────────────────────


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


@pytest.fixture
def router(tmp_path):
    return OutputRouter(reflections_dir=tmp_path / "reflections")


class TestRouteDeepReflection:
    @pytest.mark.asyncio
    async def test_routes_observations(self, db, router):
        output = DeepReflectionOutput(observations=["obs1", "obs2"])
        summary = await router.route(output, db)
        assert summary["observations_written"] == 2

        # Verify in DB (filter to reflection_observation type; excludes reflection_summary)
        cursor = await db.execute(
            "SELECT * FROM observations WHERE source = 'deep_reflection' AND type = 'reflection_observation'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_routes_learnings(self, db, router):
        output = DeepReflectionOutput(learnings=["lesson1"])
        summary = await router.route(output, db)
        assert summary["observations_written"] == 1

        cursor = await db.execute(
            "SELECT * FROM observations WHERE type = 'learning'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_routes_cognitive_state(self, db, router):
        output = DeepReflectionOutput(cognitive_state_update="new active context")
        summary = await router.route(output, db)
        assert summary["cognitive_state_updated"]

        cursor = await db.execute(
            "SELECT * FROM cognitive_state WHERE section = 'active_context'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert dict(row)["content"] == "new active context"

    @pytest.mark.asyncio
    async def test_routes_quarantines(self, db, router):
        output = DeepReflectionOutput(
            procedure_quarantines=[{"procedure_id": "p1", "reason": "failing"}]
        )
        summary = await router.route(output, db)
        assert summary["quarantines"] == 1

    @pytest.mark.asyncio
    async def test_routes_contradictions(self, db, router):
        output = DeepReflectionOutput(
            contradictions=[{"obs_a": "x", "obs_b": "y", "nature": "conflicting"}]
        )
        summary = await router.route(output, db)
        assert summary["contradictions"] == 1

    @pytest.mark.asyncio
    async def test_empty_output_detected_as_failure(self, db, router):
        output = DeepReflectionOutput()
        summary = await router.route(output, db)
        assert summary.get("empty_output") is True
        assert summary["observations_written"] == 0

    @pytest.mark.asyncio
    async def test_parse_failed_detected(self, db, router):
        output = DeepReflectionOutput(parse_failed=True)
        summary = await router.route(output, db)
        assert summary.get("parse_failed") is True
        assert summary["observations_written"] == 0


class TestRouteAssessment:
    @pytest.mark.asyncio
    async def test_stores_observation(self, db, router):
        from genesis.reflection.types import AssessmentDimension, DimensionScore

        output = WeeklyAssessmentOutput(
            dimensions=[
                DimensionScore(
                    dimension=AssessmentDimension.REFLECTION_QUALITY,
                    score=0.8, evidence="good",
                ),
            ],
            overall_score=0.8,
            observations=["doing well"],
        )
        obs_id = await router.route_assessment(output, db)
        assert obs_id

        cursor = await db.execute(
            "SELECT * FROM observations WHERE type = 'self_assessment'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_writes_markdown(self, db, router, tmp_path):
        router_with_dir = OutputRouter(reflections_dir=tmp_path / "reflections")
        output = WeeklyAssessmentOutput(overall_score=0.7)
        await router_with_dir.route_assessment(output, db)

        # Check markdown was written
        import glob
        md_files = glob.glob(str(tmp_path / "reflections" / "**" / "*.md"), recursive=True)
        assert len(md_files) == 1


class TestRouteCalibration:
    @pytest.mark.asyncio
    async def test_drift_detected_high_priority(self, db, router):
        output = QualityCalibrationOutput(drift_detected=True, observations=["drift found"])
        obs_id = await router.route_calibration(output, db)
        assert obs_id

        cursor = await db.execute(
            "SELECT * FROM observations WHERE type = 'quality_drift'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert dict(rows[0])["priority"] == "high"

    @pytest.mark.asyncio
    async def test_no_drift_medium_priority(self, db, router):
        output = QualityCalibrationOutput(drift_detected=False)
        await router.route_calibration(output, db)

        cursor = await db.execute(
            "SELECT * FROM observations WHERE type = 'quality_calibration'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert dict(rows[0])["priority"] == "medium"


# ── Reflection summary embedding tests ────────────────────────────────


class TestMarkInfluencedBatchWiring:
    @pytest.mark.asyncio
    async def test_memory_ops_mark_influenced(self, db, router):
        """OutputRouter.route() marks target_ids as influenced when memory_operations present."""
        from genesis.db.crud import observations
        from genesis.reflection.types import MemoryOperation

        # Create observations that will be referenced
        for i in range(3):
            await observations.create(
                db, id=f"inf-target-{i}", source="test", type="metric",
                content=f"obs {i}", priority="low",
                created_at=f"2026-01-01T00:00:0{i}",
            )

        output = DeepReflectionOutput(
            memory_operations=[
                MemoryOperation(
                    operation="dedup",
                    target_ids=["inf-target-0", "inf-target-1"],
                    reason="similar",
                ),
                MemoryOperation(
                    operation="prune",
                    target_ids=["inf-target-2"],
                    reason="stale",
                ),
            ],
        )
        await router.route(output, db)

        # All 3 should now have influenced_action=1
        for i in range(3):
            row = await observations.get_by_id(db, f"inf-target-{i}")
            assert row["influenced_action"] == 1, f"inf-target-{i} not marked influenced"

    @pytest.mark.asyncio
    async def test_no_memory_ops_no_influenced(self, db, router):
        """No memory_operations → mark_influenced_batch not called."""
        from genesis.db.crud import observations

        await observations.create(
            db, id="inf-noop", source="test", type="metric",
            content="x", priority="low", created_at="2026-01-01T00:00:00",
        )
        output = DeepReflectionOutput(observations=["just an obs"])
        await router.route(output, db)

        row = await observations.get_by_id(db, "inf-noop")
        assert row["influenced_action"] == 0


class TestMemoryConsolidationExecution:
    """Tests for actual dedup/merge/prune/flag_contradiction execution."""

    @pytest.mark.asyncio
    async def test_dedup_resolves_duplicates(self, db, router):
        from genesis.db.crud import observations
        from genesis.reflection.types import MemoryOperation

        for i in range(3):
            await observations.create(
                db, id=f"dup-{i}", source="test", type="metric",
                content="same thing", priority="low",
                created_at=f"2026-01-01T00:00:0{i}",
            )

        output = DeepReflectionOutput(
            memory_operations=[MemoryOperation(
                operation="dedup",
                target_ids=["dup-0", "dup-1", "dup-2"],
                reason="all identical",
            )],
        )
        summary = await router.route(output, db)
        assert summary["memory_operations"] == 1

        # First should be kept, others resolved
        r0 = await observations.get_by_id(db, "dup-0")
        assert r0["resolved"] == 0
        r1 = await observations.get_by_id(db, "dup-1")
        assert r1["resolved"] == 1
        assert "deduplicated" in r1["resolution_notes"]
        r2 = await observations.get_by_id(db, "dup-2")
        assert r2["resolved"] == 1

    @pytest.mark.asyncio
    async def test_merge_creates_combined_and_resolves(self, db, router):
        from genesis.db.crud import observations
        from genesis.reflection.types import MemoryOperation

        await observations.create(
            db, id="mg-1", source="test", type="x",
            content="part 1", priority="low", created_at="2026-01-01T00:00:00",
        )
        await observations.create(
            db, id="mg-2", source="test", type="x",
            content="part 2", priority="low", created_at="2026-01-01T00:00:01",
        )

        output = DeepReflectionOutput(
            memory_operations=[MemoryOperation(
                operation="merge",
                target_ids=["mg-1", "mg-2"],
                reason="complementary",
                merged_content="combined insight from parts 1 and 2",
            )],
        )
        await router.route(output, db)

        # Originals resolved
        for oid in ["mg-1", "mg-2"]:
            row = await observations.get_by_id(db, oid)
            assert row["resolved"] == 1
            assert "merged" in row["resolution_notes"]

        # New merged observation exists
        cursor = await db.execute(
            "SELECT * FROM observations WHERE type = 'merged_observation'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert "combined insight" in dict(rows[0])["content"]

    @pytest.mark.asyncio
    async def test_merge_skipped_when_no_merged_content(self, db, router):
        from genesis.db.crud import observations
        from genesis.reflection.types import MemoryOperation

        await observations.create(
            db, id="mg-skip", source="test", type="x",
            content="data", priority="low", created_at="2026-01-01T00:00:00",
        )

        output = DeepReflectionOutput(
            memory_operations=[MemoryOperation(
                operation="merge",
                target_ids=["mg-skip"],
                reason="should merge",
                merged_content=None,
            )],
        )
        await router.route(output, db)

        # Should NOT be resolved (merge was skipped)
        row = await observations.get_by_id(db, "mg-skip")
        assert row["resolved"] == 0

    @pytest.mark.asyncio
    async def test_prune_resolves_target(self, db, router):
        from genesis.db.crud import observations
        from genesis.reflection.types import MemoryOperation

        await observations.create(
            db, id="prune-1", source="test", type="x",
            content="stale", priority="low", created_at="2026-01-01T00:00:00",
        )

        output = DeepReflectionOutput(
            memory_operations=[MemoryOperation(
                operation="prune",
                target_ids=["prune-1"],
                reason="no longer relevant",
            )],
        )
        await router.route(output, db)

        row = await observations.get_by_id(db, "prune-1")
        assert row["resolved"] == 1
        assert "pruned" in row["resolution_notes"]

    @pytest.mark.asyncio
    async def test_flag_contradiction_creates_obs(self, db, router):
        from genesis.db.crud import observations
        from genesis.reflection.types import MemoryOperation

        await observations.create(
            db, id="contra-1", source="test", type="x",
            content="claim A", priority="low", created_at="2026-01-01T00:00:00",
        )
        await observations.create(
            db, id="contra-2", source="test", type="x",
            content="claim B", priority="low", created_at="2026-01-01T00:00:01",
        )

        output = DeepReflectionOutput(
            memory_operations=[MemoryOperation(
                operation="flag_contradiction",
                target_ids=["contra-1", "contra-2"],
                reason="A and B conflict",
            )],
        )
        await router.route(output, db)

        # Originals stay unresolved
        for oid in ["contra-1", "contra-2"]:
            row = await observations.get_by_id(db, oid)
            assert row["resolved"] == 0

        # Contradiction observation created
        cursor = await db.execute(
            "SELECT * FROM observations WHERE type = 'contradiction' AND source = 'deep_reflection'"
        )
        rows = await cursor.fetchall()
        assert len(rows) >= 1

    @pytest.mark.asyncio
    async def test_missing_target_id_skipped(self, db, router):
        from genesis.reflection.types import MemoryOperation

        output = DeepReflectionOutput(
            memory_operations=[MemoryOperation(
                operation="prune",
                target_ids=["nonexistent-id"],
                reason="stale",
            )],
        )
        # Should not raise
        summary = await router.route(output, db)
        assert summary["memory_operations"] == 1

    @pytest.mark.asyncio
    async def test_max_operations_per_cycle(self, db, router):
        from genesis.db.crud import observations
        from genesis.reflection.types import MemoryOperation

        # Create 60 observations (more than _MAX_OPS_PER_CYCLE=50)
        for i in range(60):
            await observations.create(
                db, id=f"cap-{i}", source="test", type="x",
                content=f"obs {i}", priority="low",
                created_at=f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}",
            )

        # 60 prune operations
        ops = [
            MemoryOperation(operation="prune", target_ids=[f"cap-{i}"], reason="stale")
            for i in range(60)
        ]
        output = DeepReflectionOutput(memory_operations=ops)
        await router.route(output, db)

        # Only 50 should be resolved (cap at 50)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM observations WHERE resolved = 1 AND id LIKE 'cap-%'"
        )
        resolved_count = (await cursor.fetchone())[0]
        assert resolved_count == 50

    @pytest.mark.asyncio
    async def test_audit_trail_observation_created(self, db, router):
        from genesis.db.crud import observations
        from genesis.reflection.types import MemoryOperation

        await observations.create(
            db, id="audit-1", source="test", type="x",
            content="data", priority="low", created_at="2026-01-01T00:00:00",
        )

        output = DeepReflectionOutput(
            memory_operations=[MemoryOperation(
                operation="prune", target_ids=["audit-1"], reason="stale",
            )],
        )
        await router.route(output, db)

        cursor = await db.execute(
            "SELECT * FROM observations WHERE type = 'memory_operation_executed'"
        )
        rows = await cursor.fetchall()
        assert len(rows) >= 1
        content = json.loads(dict(rows[0])["content"])
        assert content["operation"] == "prune"


class TestReflectionSummaryEmbedding:
    @pytest.mark.asyncio
    async def test_reflection_summary_stored(self, db, router):
        """Deep reflection with cognitive state and observations creates a reflection_summary."""
        output = DeepReflectionOutput(
            observations=["pattern A detected", "pattern B emerging"],
            cognitive_state_update="System is healthy and learning",
            focus_next="Monitor pattern B closely",
        )
        summary = await router.route(output, db)
        assert summary.get("reflection_summary_stored") is True

        cursor = await db.execute(
            "SELECT * FROM observations WHERE type = 'reflection_summary'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        content = dict(rows[0])["content"]
        assert "System is healthy" in content
        assert "Focus: Monitor pattern B" in content
        assert "pattern A detected" in content

    @pytest.mark.asyncio
    async def test_reflection_summary_not_stored_when_empty(self, db, router):
        """Empty reflection output does not create a reflection_summary."""
        output = DeepReflectionOutput()
        summary = await router.route(output, db)
        assert "reflection_summary_stored" not in summary

        cursor = await db.execute(
            "SELECT * FROM observations WHERE type = 'reflection_summary'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_reflection_summary_truncated(self, db, router):
        """Long summaries are truncated to 4000 chars."""
        output = DeepReflectionOutput(
            cognitive_state_update="x" * 5000,
        )
        summary = await router.route(output, db)
        assert summary.get("reflection_summary_stored") is True

        cursor = await db.execute(
            "SELECT * FROM observations WHERE type = 'reflection_summary'"
        )
        rows = await cursor.fetchall()
        content = dict(rows[0])["content"]
        assert len(content) <= 4003  # 4000 + "..."
        assert content.endswith("...")


class TestWriteObservationDedup:
    """Verify _write_observation skips duplicates via content_hash."""

    @pytest.mark.asyncio
    async def test_duplicate_observation_skipped(self, db, router):
        """Second write with identical content is deduplicated."""
        obs1 = await router._write_observation(
            db, source="test", type="test_obs", content="hello world", priority="low",
        )
        obs2 = await router._write_observation(
            db, source="test", type="test_obs", content="hello world", priority="low",
        )
        assert obs1 is not None
        assert obs2 is None  # deduplicated

        cursor = await db.execute(
            "SELECT COUNT(*) FROM observations WHERE source = 'test'"
        )
        assert (await cursor.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_different_content_not_deduplicated(self, db, router):
        obs1 = await router._write_observation(
            db, source="test", type="test_obs", content="hello", priority="low",
        )
        obs2 = await router._write_observation(
            db, source="test", type="test_obs", content="world", priority="low",
        )
        assert obs1 is not None
        assert obs2 is not None

    @pytest.mark.asyncio
    async def test_content_hash_stored(self, db, router):
        await router._write_observation(
            db, source="test", type="test_obs", content="track hash", priority="low",
        )
        cursor = await db.execute(
            "SELECT content_hash FROM observations WHERE source = 'test'"
        )
        row = await cursor.fetchone()
        assert row[0] is not None
        assert len(row[0]) == 64  # SHA-256 hex
