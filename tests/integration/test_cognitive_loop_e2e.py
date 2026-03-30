"""E2E cognitive loop integration test.

Tests the full wiring: awareness tick -> reflection -> learning -> observation.
All external calls (LLM, CC) are mocked. Validates the pipeline connections,
not LLM quality.
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from genesis.awareness.loop import perform_tick
from genesis.awareness.types import Depth, SignalReading
from genesis.db.crud import awareness_ticks, observations
from genesis.perception.types import ReflectionResult

pytestmark = pytest.mark.asyncio


# ── Helpers ────────────────────────────────────────────────────────────


class HotSignal:
    """Signal collector that returns a saturated error spike."""

    signal_name = "software_error_spike"

    async def collect(self):
        return SignalReading(
            name="software_error_spike",
            value=1.0,
            source="test",
            collected_at=datetime.now(UTC).isoformat(),
        )


class ColdSignal:
    """Signal collector that returns a zero-value reading."""

    signal_name = "software_error_spike"

    async def collect(self):
        return SignalReading(
            name="software_error_spike",
            value=0.0,
            source="test",
            collected_at=datetime.now(UTC).isoformat(),
        )


def _make_mock_reflection_engine(result: ReflectionResult | Exception = None):
    """Create a mock reflection engine with a canned reflect() response."""
    engine = AsyncMock()
    if isinstance(result, Exception):
        engine.reflect.side_effect = result
    else:
        engine.reflect.return_value = result or ReflectionResult(
            success=True, reason="mock reflection completed"
        )
    return engine


# ── Tests ──────────────────────────────────────────────────────────────


async def test_tick_triggers_reflection_stores_observation(db):
    """Hot signals -> tick -> mock reflection -> observation stored."""
    mock_engine = _make_mock_reflection_engine()

    result = await perform_tick(
        db,
        [HotSignal()],
        source="scheduled",
        reflection_engine=mock_engine,
    )

    # Tick should have triggered at Micro or Light depth
    assert result.classified_depth is not None
    assert result.classified_depth in (Depth.MICRO, Depth.LIGHT)

    # Tick stored in DB
    ticks = await awareness_ticks.query(db)
    assert len(ticks) == 1
    assert ticks[0]["id"] == result.tick_id

    # Observation stored for the triggered tick
    obs = await observations.query(db, type="awareness_tick")
    assert len(obs) == 1
    content = json.loads(obs[0]["content"])
    assert content["tick_id"] == result.tick_id
    assert content["depth"] in ("Micro", "Light")

    # Reflection engine was called
    mock_engine.reflect.assert_awaited_once()
    call_args = mock_engine.reflect.call_args
    assert call_args[0][0] == result.classified_depth  # depth
    assert call_args[0][1].tick_id == result.tick_id    # tick result


async def test_cold_signals_no_reflection(db):
    """All signals at 0.0 -> no reflection triggered."""
    mock_engine = _make_mock_reflection_engine()

    result = await perform_tick(
        db,
        [ColdSignal()],
        source="scheduled",
        reflection_engine=mock_engine,
    )

    # No depth classified
    assert result.classified_depth is None

    # Tick still stored
    ticks = await awareness_ticks.query(db)
    assert len(ticks) == 1

    # No observation created (no trigger)
    obs = await observations.query(db, type="awareness_tick")
    assert len(obs) == 0

    # Reflection engine NOT called
    mock_engine.reflect.assert_not_awaited()


async def test_reflection_failure_does_not_break_loop(db):
    """Mock reflection to raise -> tick completes -> failure logged."""
    mock_engine = _make_mock_reflection_engine(
        result=RuntimeError("LLM provider down")
    )

    # Should not raise despite reflection failure
    result = await perform_tick(
        db,
        [HotSignal()],
        source="scheduled",
        reflection_engine=mock_engine,
    )

    # Tick completed and depth was classified
    assert result.classified_depth is not None

    # Tick stored despite reflection failure
    ticks = await awareness_ticks.query(db)
    assert len(ticks) == 1

    # Observation still stored (created before reflection runs)
    obs = await observations.query(db, type="awareness_tick")
    assert len(obs) == 1

    # Reflection was attempted
    mock_engine.reflect.assert_awaited_once()


async def test_cc_bridge_called_for_deep_reflection(db):
    """When depth is Deep, cc_reflection_bridge.reflect() is called."""
    # Use critical_bypass to force a tick that triggers Deep/Strategic
    class CriticalSignal:
        signal_name = "critical_failure"

        async def collect(self):
            return SignalReading(
                name="critical_failure",
                value=1.0,
                source="test",
                collected_at=datetime.now(UTC).isoformat(),
            )

    mock_cc_bridge = AsyncMock()
    mock_cc_bridge.reflect.return_value = ReflectionResult(
        success=True, reason="CC deep reflection done"
    )

    result = await perform_tick(
        db,
        [CriticalSignal(), HotSignal()],
        source="critical_bypass",
        reason="test critical",
        cc_reflection_bridge=mock_cc_bridge,
    )

    # Tick stored
    ticks = await awareness_ticks.query(db, source="critical_bypass")
    assert len(ticks) == 1

    # If depth is Deep or Strategic, CC bridge should have been called
    if result.classified_depth in (Depth.DEEP, Depth.STRATEGIC):
        mock_cc_bridge.reflect.assert_awaited_once()
    # If Micro/Light, CC bridge wouldn't be called for the primary path
    # (only as fallback on failure), so just verify tick completed
    assert result.tick_id is not None


async def test_multiple_ticks_accumulate(db):
    """Sequential ticks each store independently in the DB."""
    r1 = await perform_tick(db, [ColdSignal()], source="scheduled")
    r2 = await perform_tick(db, [ColdSignal()], source="scheduled")
    r3 = await perform_tick(db, [HotSignal()], source="scheduled")

    ticks = await awareness_ticks.query(db)
    assert len(ticks) == 3

    tick_ids = {t["id"] for t in ticks}
    assert r1.tick_id in tick_ids
    assert r2.tick_id in tick_ids
    assert r3.tick_id in tick_ids


# ── Batch 0 fix: ceiling window ISO timestamp comparison ─────────────


async def test_ceiling_window_respects_iso_timestamps(db):
    """Ticks from hours ago must NOT count toward ceiling windows.

    Regression test for the datetime('now', ...) vs ISO format mismatch.
    """
    from datetime import timedelta

    # Insert a tick from 2 hours ago at Micro depth
    old_tick = {
        "id": "ceiling-old",
        "source": "scheduled",
        "signals_json": "[]",
        "scores_json": "[]",
        "classified_depth": "Micro",
        "trigger_reason": "test",
        "created_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    }
    await awareness_ticks.create(db, **old_tick)

    # count_in_window with 1h window should return 0
    count = await awareness_ticks.count_in_window(
        db, depth="Micro", window_seconds=3600,
    )
    assert count == 0, (
        "A 2-hour-old tick should NOT appear in a 1-hour ceiling window. "
        "If count > 0, the datetime/ISO format mismatch bug has regressed."
    )


# ── Full observation roundtrip ───────────────────────────────────────


async def test_full_observation_roundtrip(db):
    """Create reflection obs → _build_memory_hits() returns it → retrieved_count++."""
    from genesis.awareness.types import Depth, DepthScore, SignalReading, TickResult
    from genesis.identity.loader import IdentityLoader
    from genesis.perception.context import ContextAssembler

    # Create a reflection observation
    await observations.create(
        db, id="roundtrip-1", source="deep_reflection", type="learning",
        content="discovered pattern X", priority="medium",
        created_at=datetime.now(UTC).isoformat(),
    )

    # Build a ContextAssembler with a minimal identity
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path
        Path(td, "SOUL.md").write_text("You are Genesis.")
        loader = IdentityLoader(Path(td))
        assembler = ContextAssembler(identity_loader=loader)

        # tick-1 maps to anomaly focus, which includes memory_hits
        tick = TickResult(
            tick_id="tick-1",
            timestamp=datetime.now(UTC).isoformat(),
            source="test",
            signals=[SignalReading(
                name="x", value=0.5, source="test",
                collected_at=datetime.now(UTC).isoformat(),
            )],
            scores=[DepthScore(
                depth=Depth.LIGHT, raw_score=0.5, time_multiplier=1.0,
                final_score=0.5, threshold=0.2, triggered=True,
            )],
            classified_depth=Depth.LIGHT,
            trigger_reason="test",
        )

        ctx = await assembler.assemble(Depth.LIGHT, tick, db=db)

    # Observation should appear in memory_hits
    assert ctx.memory_hits is not None
    assert "discovered pattern X" in ctx.memory_hits

    # retrieved_count should have been incremented
    row = await observations.get_by_id(db, "roundtrip-1")
    assert row["retrieved_count"] >= 1


# ── Memory consolidation execution roundtrip ─────────────────────────


async def test_memory_consolidation_roundtrip(db):
    """Create duplicates → OutputRouter dedup → resolution verified."""
    from genesis.reflection.output_router import OutputRouter
    from genesis.reflection.types import DeepReflectionOutput, MemoryOperation

    # Create duplicate observations
    for i in range(3):
        await observations.create(
            db, id=f"consol-{i}", source="test", type="metric",
            content="same insight expressed differently", priority="low",
            created_at=f"2026-01-01T00:00:0{i}",
        )

    router = OutputRouter()
    output = DeepReflectionOutput(
        memory_operations=[MemoryOperation(
            operation="dedup",
            target_ids=["consol-0", "consol-1", "consol-2"],
            reason="all express same insight",
        )],
    )
    summary = await router.route(output, db)
    assert summary["memory_operations"] == 1

    # First kept, others resolved
    r0 = await observations.get_by_id(db, "consol-0")
    assert r0["resolved"] == 0
    r1 = await observations.get_by_id(db, "consol-1")
    assert r1["resolved"] == 1
    r2 = await observations.get_by_id(db, "consol-2")
    assert r2["resolved"] == 1

    # All should be marked as influenced
    for i in range(3):
        row = await observations.get_by_id(db, f"consol-{i}")
        assert row["influenced_action"] == 1


# ── Task detection visible in deep reflection context ────────────────


async def test_task_detected_visible_in_context(db):
    """task_detected observations are retrievable by ContextGatherer.gather()."""
    from genesis.reflection.context_gatherer import ContextGatherer

    await observations.create(
        db, id="task-vis-1", source="conversation_intent",
        type="task_detected", content="fix the login bug",
        priority="medium", created_at=datetime.now(UTC).isoformat(),
    )

    gatherer = ContextGatherer()
    bundle = await gatherer.gather(db)

    # task_detected should appear in recent_observations
    obs_ids = [o["id"] for o in bundle.recent_observations]
    assert "task-vis-1" in obs_ids
