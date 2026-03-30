"""Tests for PromptBuilder — template selection and rendering."""

from __future__ import annotations

from genesis.perception.types import PromptContext


def _make_context(*, depth="Micro", tick_number=0, **overrides) -> PromptContext:
    defaults = dict(
        depth=depth,
        identity="You are Genesis.",
        signals_text="cpu_usage: 0.3\nmemory_usage: 0.6",
        tick_number=tick_number,
    )
    defaults.update(overrides)
    return PromptContext(**defaults)


def test_micro_rotation_tick_0():
    from genesis.perception.prompts import PromptBuilder
    builder = PromptBuilder()
    ctx = _make_context(depth="Micro", tick_number=0)
    prompt = builder.build("Micro", ctx)
    assert "reviewing system telemetry" in prompt


def test_micro_rotation_tick_1():
    from genesis.perception.prompts import PromptBuilder
    builder = PromptBuilder()
    ctx = _make_context(depth="Micro", tick_number=1)
    prompt = builder.build("Micro", ctx)
    assert "Assume these signals are completely normal" in prompt


def test_micro_rotation_tick_2():
    from genesis.perception.prompts import PromptBuilder
    builder = PromptBuilder()
    ctx = _make_context(depth="Micro", tick_number=2)
    prompt = builder.build("Micro", ctx)
    assert "most interesting thing" in prompt


def test_micro_rotation_wraps():
    from genesis.perception.prompts import PromptBuilder
    builder = PromptBuilder()
    ctx0 = _make_context(depth="Micro", tick_number=0)
    ctx3 = _make_context(depth="Micro", tick_number=3)
    assert builder.build("Micro", ctx0) == builder.build("Micro", ctx3)


def test_light_default_situation():
    """tick_number=0 → suggested_focus="situation" (0 % 3 = 0)."""
    from genesis.perception.prompts import PromptBuilder
    builder = PromptBuilder()
    ctx = _make_context(
        depth="Light", user_profile="Timezone: EST",
        cognitive_state="Working on Phase 4.",
        suggested_focus="situation",
    )
    prompt = builder.build("Light", ctx)
    assert "SITUATION ASSESSMENT" in prompt
    assert "Working on Phase 4" in prompt


def test_light_suggested_focus_anomaly():
    from genesis.perception.prompts import PromptBuilder
    builder = PromptBuilder()
    ctx = _make_context(
        depth="Light", suggested_focus="anomaly",
        user_profile="Timezone: EST",
        cognitive_state="Working on Phase 4.",
    )
    prompt = builder.build("Light", ctx)
    assert "PATTERN DETECTION" in prompt


def test_light_suggested_focus_user_impact():
    from genesis.perception.prompts import PromptBuilder
    builder = PromptBuilder()
    ctx = _make_context(
        depth="Light", suggested_focus="user_impact",
        user_profile="Timezone: EST",
        cognitive_state="Working on Phase 4.",
    )
    prompt = builder.build("Light", ctx)
    assert "user's goals" in prompt


def test_light_focus_area_rotation():
    """30 random UUIDs should produce all 3 focus areas."""
    import uuid

    from genesis.awareness.types import Depth, TickResult
    from genesis.cc.reflection_bridge import _light_focus_area

    results = set()
    for _ in range(30):
        tick = TickResult(
            tick_id=str(uuid.uuid4()),
            timestamp="2026-03-28T12:00:00",
            source="scheduled", signals=[], scores=[],
            classified_depth=Depth.LIGHT, trigger_reason="test",
        )
        results.add(_light_focus_area(tick))
    assert results == {"situation", "user_impact", "anomaly"}


def test_variable_substitution():
    from genesis.perception.prompts import PromptBuilder
    builder = PromptBuilder()
    ctx = _make_context(depth="Micro", tick_number=0)
    prompt = builder.build("Micro", ctx)
    assert "You are Genesis." in prompt
    assert "cpu_usage: 0.3" in prompt
    assert "{identity}" not in prompt
    assert "{signals_text}" not in prompt


def test_signals_examined_substituted():
    from genesis.perception.prompts import PromptBuilder
    builder = PromptBuilder()
    ctx = _make_context(depth="Micro", tick_number=0)
    prompt = builder.build("Micro", ctx)
    assert '"signals_examined": 2' in prompt


def test_identity_override_used(tmp_path):
    """When an identity override file exists, it is used instead of templates/."""
    from genesis.perception.prompts import PromptBuilder

    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()
    override = identity_dir / "MICRO_TEMPLATE_ANALYST.md"
    override.write_text(
        "CUSTOM OVERRIDE\n{identity}\n{signals_text}\n"
        '"signals_examined": {signals_examined}',
        encoding="utf-8",
    )
    builder = PromptBuilder(identity_dir=identity_dir)
    ctx = _make_context(depth="Micro", tick_number=0)
    prompt = builder.build("Micro", ctx)
    assert "CUSTOM OVERRIDE" in prompt
    assert "You are Genesis." in prompt


def test_identity_fallback_when_no_override(tmp_path):
    """When identity dir has no override, falls back to templates/."""
    from genesis.perception.prompts import PromptBuilder

    empty_identity = tmp_path / "identity"
    empty_identity.mkdir()
    builder = PromptBuilder(identity_dir=empty_identity)
    ctx = _make_context(depth="Micro", tick_number=0)
    prompt = builder.build("Micro", ctx)
    assert "reviewing system telemetry" in prompt
