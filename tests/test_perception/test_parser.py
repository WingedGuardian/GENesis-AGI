"""Tests for OutputParser — schema validation and retry logic."""

from __future__ import annotations

import json

from genesis.perception.types import LLMResponse


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text, model="test", input_tokens=0,
        output_tokens=0, cost_usd=0.0, latency_ms=100,
    )


def test_parse_valid_micro():
    from genesis.perception.parser import OutputParser

    parser = OutputParser()
    raw = json.dumps({
        "tags": ["idle", "resource_normal"],
        "salience": 0.2,
        "anomaly": False,
        "summary": "All systems normal.",
        "signals_examined": 9,
    })
    result = parser.parse(_response(raw), "Micro")

    assert result.success is True
    assert result.output is not None
    assert result.output.tags == ["idle", "resource_normal"]
    assert result.output.salience == 0.2
    assert result.needs_retry is False


def test_parse_valid_light():
    from genesis.perception.parser import OutputParser

    raw = json.dumps({
        "assessment": "System is idle.",
        "patterns": ["declining_activity"],
        "user_model_updates": [{
            "field": "timezone",
            "value": "EST",
            "evidence": "user mentioned",
            "confidence": 0.9,
        }],
        "recommendations": ["Schedule maintenance"],
        "confidence": 0.7,
        "focus_area": "situation",
    })
    parser = OutputParser()
    result = parser.parse(_response(raw), "Light")

    assert result.success is True
    assert result.output.assessment == "System is idle."
    assert len(result.output.user_model_updates) == 1


def test_parse_invalid_json():
    from genesis.perception.parser import OutputParser

    parser = OutputParser()
    result = parser.parse(_response("not json at all"), "Micro")

    assert result.success is False
    assert result.needs_retry is True
    assert result.retry_prompt is not None
    assert "JSON" in result.retry_prompt


def test_parse_missing_required_field():
    from genesis.perception.parser import OutputParser

    raw = json.dumps({"tags": ["idle"]})
    parser = OutputParser()
    result = parser.parse(_response(raw), "Micro")

    assert result.success is False
    assert result.needs_retry is True
    assert "salience" in result.retry_prompt


def test_parse_salience_out_of_range():
    from genesis.perception.parser import OutputParser

    raw = json.dumps({
        "tags": ["idle"],
        "salience": 1.5,
        "anomaly": False,
        "summary": "Normal.",
        "signals_examined": 5,
    })
    parser = OutputParser()
    result = parser.parse(_response(raw), "Micro")

    assert result.success is False
    assert result.needs_retry is True


def test_parse_extracts_json_from_markdown():
    from genesis.perception.parser import OutputParser

    text = 'Here is the analysis:\n```json\n{"tags": ["idle"], "salience": 0.1, "anomaly": false, "summary": "Normal.", "signals_examined": 5}\n```'
    parser = OutputParser()
    result = parser.parse(_response(text), "Micro")

    assert result.success is True
    assert result.output.tags == ["idle"]


def test_parse_empty_tags_allowed():
    from genesis.perception.parser import OutputParser

    raw = json.dumps({
        "tags": [],
        "salience": 0.0,
        "anomaly": False,
        "summary": "Nothing noteworthy.",
        "signals_examined": 3,
    })
    parser = OutputParser()
    result = parser.parse(_response(raw), "Micro")

    assert result.success is True
    assert result.output.tags == []


def test_parse_light_dict_patterns_coerced_to_str():
    """When LLM returns patterns as dicts, they should be coerced to strings."""
    from genesis.perception.parser import OutputParser

    raw = json.dumps({
        "assessment": "System is idle.",
        "patterns": [
            {"name": "declining_activity", "description": "Less usage"},
            "normal_string_pattern",
        ],
        "user_model_updates": [],
        "recommendations": [],
        "confidence": 0.7,
        "focus_area": "situation",
    })
    parser = OutputParser()
    result = parser.parse(_response(raw), "Light")

    assert result.success is True
    # Both patterns should be strings — dict coerced, string unchanged
    for p in result.output.patterns:
        assert isinstance(p, str), f"Pattern should be str, got {type(p)}: {p}"
    assert "declining_activity" in result.output.patterns[0]
    assert result.output.patterns[1] == "normal_string_pattern"
