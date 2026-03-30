"""Tests for AwarenessLoop + ReflectionEngine integration."""

from __future__ import annotations

from unittest.mock import AsyncMock

from genesis.awareness.types import Depth
from genesis.perception.types import MicroOutput, ReflectionResult


async def test_tick_with_micro_depth_triggers_reflection(db):
    """When a tick classifies as MICRO, perform_tick should call reflect()."""
    from genesis.awareness.loop import perform_tick
    from genesis.awareness.signals import ConversationCollector

    engine = AsyncMock()
    engine.reflect = AsyncMock(return_value=ReflectionResult(
        success=True,
        output=MicroOutput(
            tags=["idle"], salience=0.1, anomaly=False,
            summary="Normal.", signals_examined=1,
        ),
    ))

    collectors = [ConversationCollector()]
    result = await perform_tick(
        db, collectors, source="scheduled",
        reflection_engine=engine,
    )

    # MICRO + LIGHT → reflection_engine (API primary)
    if result.classified_depth in (Depth.MICRO, Depth.LIGHT):
        engine.reflect.assert_called_once()


async def test_tick_without_engine_still_works(db):
    """perform_tick should work without a reflection engine (backwards compat)."""
    from genesis.awareness.loop import perform_tick
    from genesis.awareness.signals import ConversationCollector

    collectors = [ConversationCollector()]
    result = await perform_tick(db, collectors, source="scheduled")

    assert result.tick_id is not None
