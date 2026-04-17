"""Tests for overhauled CCReflectionBridge (Phase 7)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.awareness.types import Depth, TickResult
from genesis.cc.reflection_bridge import CCReflectionBridge
from genesis.db.schema import create_all_tables, seed_data
from genesis.reflection.context_gatherer import ContextGatherer
from genesis.reflection.output_router import OutputRouter


@dataclass
class FakeCCOutput:
    text: str = '{"observations": ["test obs"], "confidence": 0.8}'
    is_error: bool = False
    error_message: str = ""
    model_used: str = "sonnet"
    cost_usd: float = 0.01
    input_tokens: int = 100
    output_tokens: int = 50
    model_requested: str = ""
    downgraded: bool = False
    session_id: str = "fake-cc-session"


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


@pytest.fixture
def mock_session_manager():
    mgr = AsyncMock()
    mgr.create_background.return_value = {"id": "sess-1"}
    return mgr


@pytest.fixture
def mock_invoker():
    inv = AsyncMock()
    inv.run.return_value = FakeCCOutput()
    return inv


def _make_tick():
    return TickResult(
        tick_id=str(uuid.uuid4()),
        timestamp=datetime.now(UTC).isoformat(),
        source="scheduled",
        signals=[],
        scores=[],
        classified_depth=Depth.DEEP,
        trigger_reason="test",
    )


class TestReflectLegacy:
    """Bridge without Phase 7 components falls back to legacy behavior."""

    @pytest.mark.asyncio
    async def test_legacy_reflect(self, db, mock_session_manager, mock_invoker):
        bridge = CCReflectionBridge(
            session_manager=mock_session_manager,
            invoker=mock_invoker,
            db=db,
        )
        result = await bridge.reflect(Depth.DEEP, _make_tick(), db=db)
        assert result.success
        mock_invoker.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_legacy_stores_observation(self, db, mock_session_manager, mock_invoker):
        bridge = CCReflectionBridge(
            session_manager=mock_session_manager,
            invoker=mock_invoker,
            db=db,
        )
        await bridge.reflect(Depth.DEEP, _make_tick(), db=db)

        cursor = await db.execute(
            "SELECT * FROM observations WHERE source = 'cc_reflection_deep' AND type = 'reflection_output'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1


class TestReflectEnriched:
    """Bridge with Phase 7 components uses enriched path."""

    @pytest.mark.asyncio
    async def test_enriched_reflect_routes_output(self, db, mock_session_manager, mock_invoker, tmp_path):
        gatherer = ContextGatherer()
        router = OutputRouter(reflections_dir=tmp_path / "reflections")

        bridge = CCReflectionBridge(
            session_manager=mock_session_manager,
            invoker=mock_invoker,
            db=db,
            context_gatherer=gatherer,
            output_router=router,
        )

        # Need some observations to trigger pending work
        now = datetime.now(UTC).isoformat()
        for _i in range(12):
            await db.execute(
                "INSERT INTO observations (id, source, type, content, priority, created_at) "
                "VALUES (?, 'test', 'test', 'content', 'low', ?)",
                (str(uuid.uuid4()), now),
            )
        await db.commit()

        result = await bridge.reflect(Depth.DEEP, _make_tick(), db=db)
        assert result.success
        mock_invoker.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_when_no_pending_work(self, db, mock_session_manager, mock_invoker):
        gatherer = ContextGatherer()
        router = OutputRouter()

        # Seed a fresh cognitive state so cognitive_regeneration=False
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO cognitive_state (id, section, content, created_at) "
            "VALUES ('test-cog', 'active_context', 'test state', ?)",
            (now,),
        )
        await db.commit()

        bridge = CCReflectionBridge(
            session_manager=mock_session_manager,
            invoker=mock_invoker,
            db=db,
            context_gatherer=gatherer,
            output_router=router,
        )

        result = await bridge.reflect(Depth.DEEP, _make_tick(), db=db)
        assert result.success
        assert "No pending work" in result.reason
        mock_invoker.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_strategic_skips_pending_check(self, db, mock_session_manager, mock_invoker):
        """Strategic reflection always runs (no pending work gate)."""
        gatherer = ContextGatherer()
        router = OutputRouter()

        bridge = CCReflectionBridge(
            session_manager=mock_session_manager,
            invoker=mock_invoker,
            db=db,
            context_gatherer=gatherer,
            output_router=router,
        )

        result = await bridge.reflect(Depth.STRATEGIC, _make_tick(), db=db)
        assert result.success
        mock_invoker.run.assert_called_once()


class TestReflectErrorHandling:
    @pytest.mark.asyncio
    async def test_session_creation_failure(self, db, mock_invoker):
        mgr = AsyncMock()
        mgr.create_background.side_effect = RuntimeError("DB error")
        bridge = CCReflectionBridge(
            session_manager=mgr, invoker=mock_invoker, db=db,
        )
        result = await bridge.reflect(Depth.DEEP, _make_tick(), db=db)
        assert not result.success

    @pytest.mark.asyncio
    async def test_cc_invocation_failure(self, db, mock_session_manager):
        inv = AsyncMock()
        inv.run.return_value = FakeCCOutput(is_error=True, error_message="timeout")
        bridge = CCReflectionBridge(
            session_manager=mock_session_manager, invoker=inv, db=db,
        )
        result = await bridge.reflect(Depth.DEEP, _make_tick(), db=db)
        assert not result.success
        mock_session_manager.fail.assert_called_once()


class TestWeeklyAssessment:
    @pytest.mark.asyncio
    async def test_no_context_gatherer(self, db, mock_session_manager, mock_invoker):
        bridge = CCReflectionBridge(
            session_manager=mock_session_manager, invoker=mock_invoker, db=db,
        )
        result = await bridge.run_weekly_assessment(db)
        assert not result.success
        assert "No context gatherer" in result.reason

    @pytest.mark.asyncio
    async def test_assessment_runs(self, db, mock_session_manager, mock_invoker, tmp_path):
        mock_invoker.run.return_value = FakeCCOutput(
            text=json.dumps({
                "dimensions": [{"dimension": "reflection_quality", "score": 0.8}],
                "overall_score": 0.8,
                "observations": ["test"],
            })
        )
        gatherer = ContextGatherer()
        router = OutputRouter(reflections_dir=tmp_path / "reflections")

        bridge = CCReflectionBridge(
            session_manager=mock_session_manager, invoker=mock_invoker,
            db=db, context_gatherer=gatherer, output_router=router,
        )
        result = await bridge.run_weekly_assessment(db)
        assert result.success

    @pytest.mark.asyncio
    async def test_assessment_cc_failure(self, db, mock_session_manager):
        inv = AsyncMock()
        inv.run.return_value = FakeCCOutput(is_error=True, error_message="err")
        gatherer = ContextGatherer()

        bridge = CCReflectionBridge(
            session_manager=mock_session_manager, invoker=inv,
            db=db, context_gatherer=gatherer,
        )
        result = await bridge.run_weekly_assessment(db)
        assert not result.success


class TestQualityCalibration:
    @pytest.mark.asyncio
    async def test_calibration_runs(self, db, mock_session_manager, mock_invoker, tmp_path):
        mock_invoker.run.return_value = FakeCCOutput(
            text=json.dumps({"drift_detected": False, "observations": ["stable"]})
        )
        gatherer = ContextGatherer()
        router = OutputRouter(reflections_dir=tmp_path / "reflections")

        bridge = CCReflectionBridge(
            session_manager=mock_session_manager, invoker=mock_invoker,
            db=db, context_gatherer=gatherer, output_router=router,
        )
        result = await bridge.run_quality_calibration(db)
        assert result.success

    @pytest.mark.asyncio
    async def test_calibration_no_gatherer(self, db, mock_session_manager, mock_invoker):
        bridge = CCReflectionBridge(
            session_manager=mock_session_manager, invoker=mock_invoker, db=db,
        )
        result = await bridge.run_quality_calibration(db)
        assert not result.success


class TestPromptFiles:
    def test_reflection_deep_loads(self):
        path = Path(__file__).resolve().parent.parent.parent / "src" / "genesis" / "identity" / "REFLECTION_DEEP.md"
        content = path.read_text()
        assert "Memory Consolidation" in content
        assert "Surplus Review" in content
        assert "Skill Review" in content
        assert "Cognitive State Regeneration" in content
        assert "memory_operations" in content

    def test_self_assessment_loads(self):
        path = Path(__file__).resolve().parent.parent.parent / "src" / "genesis" / "identity" / "SELF_ASSESSMENT.md"
        content = path.read_text()
        assert "Reflection Quality" in content
        assert "Procedure Effectiveness" in content
        assert "insufficient data" in content.lower()

    def test_quality_calibration_loads(self):
        path = Path(__file__).resolve().parent.parent.parent / "src" / "genesis" / "identity" / "QUALITY_CALIBRATION.md"
        content = path.read_text()
        assert "drift_detected" in content
        assert "quarantine" in content.lower()


class TestLateBinding:
    def test_set_context_gatherer(self, mock_session_manager, mock_invoker):
        bridge = CCReflectionBridge(
            session_manager=mock_session_manager, invoker=mock_invoker, db=None,
        )
        assert bridge._context_gatherer is None
        gatherer = ContextGatherer()
        bridge.set_context_gatherer(gatherer)
        assert bridge._context_gatherer is gatherer

    def test_set_output_router(self, mock_session_manager, mock_invoker):
        bridge = CCReflectionBridge(
            session_manager=mock_session_manager, invoker=mock_invoker, db=None,
        )
        assert bridge._output_router is None
        router = OutputRouter()
        bridge.set_output_router(router)
        assert bridge._output_router is router
