"""Tests for light reflection surplus candidate flagging."""

from __future__ import annotations

import json

from genesis.awareness.types import Depth, SignalReading, TickResult
from genesis.perception.types import LightOutput, LLMResponse


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text, model="test", input_tokens=0,
        output_tokens=0, cost_usd=0.0, latency_ms=100,
    )


def _make_tick() -> TickResult:
    return TickResult(
        tick_id="tick-1",
        timestamp="2026-03-05T10:00:00+00:00",
        source="scheduled",
        signals=[
            SignalReading(
                name="cpu_usage", value=0.3, source="system",
                collected_at="2026-03-05T10:00:00+00:00",
            ),
        ],
        scores=[],
        classified_depth=Depth.MICRO,
        trigger_reason="threshold_exceeded",
    )


def _make_light(**overrides) -> LightOutput:
    defaults = dict(
        assessment="System is idle.",
        patterns=["declining_activity"],
        user_model_updates=[],
        recommendations=["Schedule maintenance"],
        confidence=0.7,
        focus_area="situation",
    )
    defaults.update(overrides)
    return LightOutput(**defaults)


# --- Type tests ---

def test_light_output_surplus_candidates_field():
    output = _make_light(surplus_candidates=["investigate X", "audit Y"])
    assert output.surplus_candidates == ["investigate X", "audit Y"]


def test_light_output_surplus_candidates_default():
    output = _make_light()
    assert output.surplus_candidates == []


# --- Parser tests ---

def test_parser_extracts_surplus_candidates():
    from genesis.perception.parser import OutputParser

    raw = json.dumps({
        "assessment": "System is idle.",
        "patterns": ["declining_activity"],
        "user_model_updates": [],
        "recommendations": ["Schedule maintenance"],
        "confidence": 0.7,
        "focus_area": "situation",
        "surplus_candidates": ["investigate memory staleness", "audit procedure P-42"],
    })
    parser = OutputParser()
    result = parser.parse(_response(raw), "Light")

    assert result.success is True
    assert result.output.surplus_candidates == [
        "investigate memory staleness",
        "audit procedure P-42",
    ]


def test_parser_defaults_empty_candidates():
    from genesis.perception.parser import OutputParser

    raw = json.dumps({
        "assessment": "System is idle.",
        "patterns": [],
        "user_model_updates": [],
        "recommendations": [],
        "confidence": 0.5,
        "focus_area": "situation",
    })
    parser = OutputParser()
    result = parser.parse(_response(raw), "Light")

    assert result.success is True
    assert result.output.surplus_candidates == []


# --- Writer tests ---

async def test_writer_stores_surplus_candidates_in_insights(db):
    """Surplus candidates now write to surplus_insights table, not observations."""
    from genesis.db.crud import surplus as surplus_crud
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = _make_light(surplus_candidates=["investigate memory staleness", "audit procedure P-42"])
    await writer.write(output, Depth.LIGHT, _make_tick(), db=db)

    pending = await surplus_crud.list_pending(db)
    assert len(pending) == 2
    contents = {r["content"] for r in pending}
    assert "investigate memory staleness" in contents
    assert "audit procedure P-42" in contents
    assert all(r["source_task_type"] == "light_reflection_candidate" for r in pending)


async def test_writer_skips_empty_candidates(db):
    from genesis.db.crud import surplus as surplus_crud
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = _make_light(surplus_candidates=["valid candidate", "", "  ", "another valid"])
    await writer.write(output, Depth.LIGHT, _make_tick(), db=db)

    pending = await surplus_crud.list_pending(db)
    assert len(pending) == 2
    contents = {r["content"] for r in pending}
    assert "valid candidate" in contents
    assert "another valid" in contents
