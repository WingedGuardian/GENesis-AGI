"""Tests for memory extraction prompt and parser."""

from __future__ import annotations

import pytest

from genesis.memory.extraction import (
    Extraction,
    build_extraction_prompt,
    extractions_to_store_kwargs,
    parse_extraction_response,
)


class TestParseExtractionResponse:
    """Tests for parse_extraction_response."""

    def test_valid_json_in_backticks(self):
        text = """Here are the extractions:
```json
[
  {
    "content": "Agentmail evaluated for Genesis outreach",
    "type": "evaluation",
    "confidence": 0.9,
    "entities": ["Agentmail"],
    "relationships": [
      {"from": "Agentmail", "to": "Genesis outreach", "type": "evaluated_for"}
    ],
    "temporal": "2026-03-17"
  }
]
```
"""
        result = parse_extraction_response(text)
        assert len(result) == 1
        assert result[0].content == "Agentmail evaluated for Genesis outreach"
        assert result[0].extraction_type == "evaluation"
        assert result[0].confidence == 0.9
        assert result[0].entities == ["Agentmail"]
        assert len(result[0].relationships) == 1
        assert result[0].relationships[0]["type"] == "evaluated_for"
        assert result[0].temporal == "2026-03-17"

    def test_empty_array(self):
        result = parse_extraction_response("```json\n[]\n```")
        assert result == []

    def test_malformed_json_raises(self):
        with pytest.raises(ValueError, match="Failed to parse"):
            parse_extraction_response("```json\n{not valid json}\n```")

    def test_object_without_extractions_returns_empty(self):
        # A dict without "extractions" key returns empty (new format compat)
        result = parse_extraction_response('```json\n{"key": "value"}\n```')
        assert result == []

    def test_object_with_extractions_key_accepted(self):
        text = '```json\n{"extractions": [{"content": "test", "type": "entity", "confidence": 0.8}], "session_keywords": ["test"], "session_topic": "testing"}\n```'
        result = parse_extraction_response(text)
        assert len(result) == 1
        assert result[0].content == "test"

    def test_full_response_returns_keywords_and_topic(self):
        from genesis.memory.extraction import parse_extraction_response_full
        text = '```json\n{"extractions": [{"content": "Guardian setup", "type": "entity", "confidence": 0.8}], "session_keywords": ["Guardian", "SSH"], "session_topic": "Guardian bidirectional monitoring"}\n```'
        result = parse_extraction_response_full(text)
        assert result.session_keywords == ["guardian", "ssh"]
        assert result.session_topic == "Guardian bidirectional monitoring"
        assert len(result.extractions) == 1

    def test_full_response_legacy_array_has_empty_keywords(self):
        from genesis.memory.extraction import parse_extraction_response_full
        text = '```json\n[{"content": "test", "type": "entity", "confidence": 0.8}]\n```'
        result = parse_extraction_response_full(text)
        assert result.session_keywords == []
        assert result.session_topic == ""
        assert len(result.extractions) == 1

    def test_confidence_clamping(self):
        text = '```json\n[{"content": "test", "type": "entity", "confidence": 1.5}]\n```'
        result = parse_extraction_response(text)
        assert result[0].confidence == 1.0

        text2 = '```json\n[{"content": "test", "type": "entity", "confidence": -0.5}]\n```'
        result2 = parse_extraction_response(text2)
        assert result2[0].confidence == 0.0

    def test_invalid_confidence_type_defaults(self):
        text = '```json\n[{"content": "test", "type": "entity", "confidence": "high"}]\n```'
        result = parse_extraction_response(text)
        assert result[0].confidence == 0.5

    def test_invalid_type_defaults_to_entity(self):
        text = '```json\n[{"content": "test", "type": "unknown_type", "confidence": 0.8}]\n```'
        result = parse_extraction_response(text)
        assert result[0].extraction_type == "entity"

    def test_skips_items_without_content(self):
        text = '```json\n[{"type": "entity"}, {"content": "real item", "type": "entity"}]\n```'
        result = parse_extraction_response(text)
        assert len(result) == 1
        assert result[0].content == "real item"

    def test_relationship_validation(self):
        text = """```json
[{"content": "test", "type": "entity", "confidence": 0.8, "relationships": [
  {"from": "A", "to": "B", "type": "related_to"},
  {"invalid": "structure"},
  {"from": "C", "to": "D"}
]}]
```"""
        result = parse_extraction_response(text)
        # Only the first relationship is valid (has from, to, type)
        assert len(result[0].relationships) == 1
        assert result[0].relationships[0]["from"] == "A"

    def test_non_list_entities_defaults(self):
        text = '```json\n[{"content": "test", "type": "entity", "entities": "not a list"}]\n```'
        result = parse_extraction_response(text)
        assert result[0].entities == []

    def test_raw_json_without_backticks(self):
        text = '[{"content": "test", "type": "entity", "confidence": 0.7}]'
        result = parse_extraction_response(text)
        assert len(result) == 1
        assert result[0].content == "test"

    def test_multiple_extractions(self):
        text = """```json
[
  {"content": "First item", "type": "decision", "confidence": 0.8},
  {"content": "Second item", "type": "action_item", "confidence": 0.6}
]
```"""
        result = parse_extraction_response(text)
        assert len(result) == 2
        assert result[0].extraction_type == "decision"
        assert result[1].extraction_type == "action_item"


class TestBuildExtractionPrompt:
    """Tests for build_extraction_prompt."""

    def test_includes_conversation_text(self):
        prompt = build_extraction_prompt("Hello from the user")
        assert "Hello from the user" in prompt
        assert "JSON object" in prompt
        assert "entities" in prompt
        assert "session_keywords" in prompt

    def test_template_variables_replaced(self):
        prompt = build_extraction_prompt("test text")
        assert "{conversation_text}" not in prompt


class TestExtractionsToStoreKwargs:
    """Tests for extractions_to_store_kwargs."""

    def test_basic_mapping(self):
        extraction = Extraction(
            content="Agentmail is an email service",
            extraction_type="evaluation",
            confidence=0.9,
            entities=["Agentmail"],
            relationships=[],
            temporal="2026-03-17",
        )
        kwargs = extractions_to_store_kwargs(
            extraction,
            source_session_id="sess-123",
            transcript_path="/path/to/transcript.jsonl",
            source_line_range=(10, 50),
        )
        assert kwargs["content"] == "Agentmail is an email service"
        assert kwargs["source"] == "session_extraction"
        assert kwargs["memory_type"] == "episodic"
        assert kwargs["confidence"] == 0.9
        assert "Agentmail" in kwargs["tags"]
        assert "evaluation" in kwargs["tags"]
        assert "2026-03-17" in kwargs["tags"]
        assert kwargs["source_session_id"] == "sess-123"
        assert kwargs["transcript_path"] == "/path/to/transcript.jsonl"
        assert kwargs["source_line_range"] == (10, 50)
        assert kwargs["extraction_timestamp"] is not None

    def test_no_temporal_tag(self):
        extraction = Extraction(
            content="Some fact",
            extraction_type="entity",
            confidence=0.5,
        )
        kwargs = extractions_to_store_kwargs(extraction)
        assert "entity" in kwargs["tags"]
        # No temporal tag when temporal is None
        assert len(kwargs["tags"]) == 1
