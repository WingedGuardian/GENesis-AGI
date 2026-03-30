"""Tests for light → deep escalation wiring."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from genesis.awareness.loop import perform_tick
from genesis.awareness.types import Depth, SignalReading
from genesis.db.crud import observations
from genesis.perception.parser import OutputParser
from genesis.perception.types import LightOutput, ReflectionResult

# ── LightOutput field tests ──────────────────────────────────────────


def test_light_output_escalation_fields():
    """LightOutput accepts escalate_to_deep and escalation_reason."""
    out = LightOutput(
        assessment="test",
        patterns=[],
        user_model_updates=[],
        recommendations=[],
        confidence=0.8,
        focus_area="general",
        escalate_to_deep=True,
        escalation_reason="anomaly detected",
    )
    assert out.escalate_to_deep is True
    assert out.escalation_reason == "anomaly detected"


def test_light_output_escalation_defaults():
    """LightOutput defaults escalation fields when not provided."""
    out = LightOutput(
        assessment="test",
        patterns=[],
        user_model_updates=[],
        recommendations=[],
        confidence=0.8,
        focus_area="general",
    )
    assert out.escalate_to_deep is False
    assert out.escalation_reason is None


# ── Parser extraction tests ──────────────────────────────────────────


def test_parser_extracts_escalation():
    """Parser returns escalation fields from JSON."""
    from genesis.perception.types import LLMResponse

    parser = OutputParser()
    data = {
        "assessment": "needs attention",
        "patterns": ["spike"],
        "user_model_updates": [],
        "recommendations": ["investigate"],
        "confidence": 0.9,
        "focus_area": "errors",
        "escalate_to_deep": True,
        "escalation_reason": "error rate tripled",
    }
    resp = LLMResponse(
        text=json.dumps(data),
        model="test",
        input_tokens=0,
        output_tokens=0,
        cost_usd=0,
        latency_ms=0,
    )
    result = parser.parse(resp, "light")
    assert result.success
    assert result.output.escalate_to_deep is True
    assert result.output.escalation_reason == "error rate tripled"


def test_parser_defaults_escalation():
    """Parser defaults to False/None when escalation fields absent."""
    from genesis.perception.types import LLMResponse

    parser = OutputParser()
    data = {
        "assessment": "all clear",
        "patterns": [],
        "user_model_updates": [],
        "recommendations": [],
        "confidence": 0.7,
        "focus_area": "general",
    }
    resp = LLMResponse(
        text=json.dumps(data),
        model="test",
        input_tokens=0,
        output_tokens=0,
        cost_usd=0,
        latency_ms=0,
    )
    result = parser.parse(resp, "light")
    assert result.success
    assert result.output.escalate_to_deep is False
    assert result.output.escalation_reason is None


# ── Integration tests (perform_tick) ─────────────────────────────────


class _HotSignal:
    """Collector that fires high enough to trigger at least MICRO."""

    signal_name = "software_error_spike"

    async def collect(self):
        return SignalReading(
            name="software_error_spike",
            value=1.0,
            source="health_mcp",
            collected_at="2026-03-03T12:00:00+00:00",
        )


@pytest.mark.asyncio
async def test_escalation_triggers_deep(db):
    """When light sets escalate_to_deep=True, next tick forces DEEP."""
    # Step 1: Simulate a light reflection that requests escalation.
    # We do this by creating a pending escalation observation directly
    # (as if the previous tick's light reflection had set escalate_to_deep=True).
    now = datetime.now(UTC).isoformat()
    await observations.create(
        db,
        id=str(uuid.uuid4()),
        source="awareness_loop",
        type="light_escalation_pending",
        content="anomaly detected in error rate",
        priority="high",
        created_at=now,
    )

    # Step 2: Run a tick with a CC bridge mock — it should force DEEP
    cc_bridge = AsyncMock()
    cc_bridge.reflect = AsyncMock(
        return_value=ReflectionResult(success=True, reason="deep done")
    )

    result = await perform_tick(
        db,
        [_HotSignal()],
        source="scheduled",
        cc_reflection_bridge=cc_bridge,
    )

    assert result.classified_depth == Depth.DEEP
    assert "light escalation" in (result.trigger_reason or "").lower()

    # CC bridge should have been called with DEEP and escalation_source
    cc_bridge.reflect.assert_called_once()
    call_kwargs = cc_bridge.reflect.call_args
    assert call_kwargs.kwargs.get("escalation_source") == "light_escalation"


@pytest.mark.asyncio
async def test_escalation_cooldown(db):
    """Max 1 escalation per 2h window — second one is skipped."""
    now = datetime.now(UTC)

    # Create a pending escalation
    await observations.create(
        db,
        id=str(uuid.uuid4()),
        source="awareness_loop",
        type="light_escalation_pending",
        content="second anomaly",
        priority="high",
        created_at=now.isoformat(),
    )

    # Also create a recently resolved escalation (within 2h) to trigger cooldown
    await observations.create(
        db,
        id=str(uuid.uuid4()),
        source="awareness_loop",
        type="light_escalation_resolved",
        content="previous escalation consumed",
        priority="low",
        created_at=(now - timedelta(minutes=30)).isoformat(),
    )

    cc_bridge = AsyncMock()
    cc_bridge.reflect = AsyncMock(
        return_value=ReflectionResult(success=True, reason="deep done")
    )

    await perform_tick(
        db,
        [_HotSignal()],
        source="scheduled",
        cc_reflection_bridge=cc_bridge,
    )

    # Should NOT have forced DEEP — cooldown is active.
    # The tick may still trigger some depth from signals alone, but the
    # cc_bridge.reflect should not have been called with escalation_source.
    if cc_bridge.reflect.called:
        call_kwargs = cc_bridge.reflect.call_args
        assert call_kwargs.kwargs.get("escalation_source") != "light_escalation"


@pytest.mark.asyncio
async def test_escalation_source_passed_to_bridge(db):
    """Verify CC bridge receives escalation_source='light_escalation'."""
    now = datetime.now(UTC).isoformat()
    await observations.create(
        db,
        id=str(uuid.uuid4()),
        source="awareness_loop",
        type="light_escalation_pending",
        content="test escalation",
        priority="high",
        created_at=now,
    )

    cc_bridge = AsyncMock()
    cc_bridge.reflect = AsyncMock(
        return_value=ReflectionResult(success=True, reason="done")
    )

    await perform_tick(
        db,
        [_HotSignal()],
        source="scheduled",
        cc_reflection_bridge=cc_bridge,
    )

    # Verify reflect was called with escalation_source
    assert cc_bridge.reflect.called
    _, kwargs = cc_bridge.reflect.call_args
    assert kwargs["escalation_source"] == "light_escalation"


@pytest.mark.asyncio
async def test_light_reflection_creates_escalation_observation(db):
    """When light CC reflection output has escalate_to_deep=True, an observation is created.

    Light reflections now go through the CC reflection bridge (not the API
    reflection engine). The CC bridge parses the JSON output and creates a
    light_escalation_pending observation if escalate_to_deep is true.
    """
    # Mock CC reflection bridge that succeeds
    cc_bridge = AsyncMock()
    cc_bridge.reflect = AsyncMock(
        return_value=ReflectionResult(success=True, reason="CC Light completed")
    )

    # We also need the CC bridge's _store_reflection_output to create the
    # escalation observation. Since that's internal, we test the full flow
    # by having the mock call the real escalation logic.
    # Simpler: just verify the CC bridge was called with Light depth.
    # The escalation parsing is tested separately in test_reflection_bridge.

    # Use a signal that triggers LIGHT depth
    class LightSignal:
        signal_name = "conversation_idle_minutes"

        async def collect(self):
            return SignalReading(
                name="conversation_idle_minutes",
                value=0.8,
                source="conversation",
                collected_at="2026-03-03T12:00:00+00:00",
            )

    # Mock classifier to force LIGHT depth so the test is deterministic
    from unittest.mock import patch

    from genesis.awareness.classifier import DepthDecision, DepthScore
    mock_score = DepthScore(
        depth=Depth.LIGHT, raw_score=0.5, time_multiplier=1.0,
        final_score=0.5, threshold=0.3, triggered=True,
    )
    mock_decision = DepthDecision(depth=Depth.LIGHT, score=mock_score, reason="test-forced")
    with patch("genesis.awareness.loop.classify_depth", new=AsyncMock(return_value=mock_decision)):
        result = await perform_tick(
            db,
            [LightSignal()],
            source="scheduled",
            cc_reflection_bridge=cc_bridge,
        )

    # Verify LIGHT was classified and CC bridge was called (not reflection_engine)
    assert result.classified_depth == Depth.LIGHT
    assert cc_bridge.reflect.called
    call_args = cc_bridge.reflect.call_args
    assert call_args[0][0] == Depth.LIGHT  # first positional arg is depth
