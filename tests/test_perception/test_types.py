"""Tests for perception type definitions."""

from __future__ import annotations


def test_micro_output_frozen():
    from genesis.perception.types import MicroOutput

    output = MicroOutput(
        tags=["resource_normal", "schedule_idle"],
        salience=0.3,
        anomaly=False,
        summary="All signals within normal range.",
        signals_examined=9,
    )
    assert output.tags == ["resource_normal", "schedule_idle"]
    assert output.salience == 0.3
    assert output.anomaly is False
    assert output.signals_examined == 9

    import pytest
    with pytest.raises(AttributeError):
        output.salience = 0.5  # frozen


def test_light_output_frozen():
    from genesis.perception.types import LightOutput, UserModelDelta

    delta = UserModelDelta(
        field="timezone", value="EST", evidence="user mentioned EST", confidence=0.9,
    )
    output = LightOutput(
        assessment="System is idle, user inactive.",
        patterns=["declining_activity"],
        user_model_updates=[delta],
        recommendations=["Schedule maintenance during idle period"],
        confidence=0.7,
        focus_area="situation",
    )
    assert output.focus_area == "situation"
    assert len(output.user_model_updates) == 1
    assert output.user_model_updates[0].field == "timezone"


def test_reflection_result_success():
    from genesis.perception.types import MicroOutput, ReflectionResult

    output = MicroOutput(
        tags=["idle"], salience=0.1, anomaly=False,
        summary="Normal.", signals_examined=5,
    )
    result = ReflectionResult(success=True, output=output)
    assert result.success is True
    assert result.output is not None
    assert result.reason is None


def test_reflection_result_failure():
    from genesis.perception.types import ReflectionResult

    result = ReflectionResult(success=False, reason="all_providers_exhausted")
    assert result.success is False
    assert result.output is None
    assert result.reason == "all_providers_exhausted"


def test_prompt_context():
    from genesis.perception.types import PromptContext

    ctx = PromptContext(
        depth="micro",
        identity="You are Genesis...",
        signals_text="cpu: 0.3, memory: 0.6",
        tick_number=42,
    )
    assert ctx.depth == "micro"
    assert ctx.tick_number == 42
    assert ctx.user_profile is None
    assert ctx.cognitive_state is None
    assert ctx.memory_hits is None


def test_llm_response():
    from genesis.perception.types import LLMResponse

    resp = LLMResponse(
        text='{"tags": ["idle"]}',
        model="groq-free",
        input_tokens=500,
        output_tokens=100,
        cost_usd=0.0,
        latency_ms=320,
    )
    assert resp.cost_usd == 0.0
    assert resp.model == "groq-free"
