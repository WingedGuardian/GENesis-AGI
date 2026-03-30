"""Tests for AZ ↔ Genesis memory adapter."""

from __future__ import annotations

from genesis.memory.az_adapter import (
    _generate_id,
    doc_to_payload,
    extract_area_filter,
    memory_subdir_to_collection,
    payload_to_doc,
)


def test_doc_to_payload_basic():
    metadata = {
        "id": "abc123",
        "timestamp": "2026-03-01 12:30:00",
        "area": "fragments",
        "tags": ["tool", "python"],
    }
    result = doc_to_payload("hello world", metadata)
    assert result["content"] == "hello world"
    assert result["memory_id"] == "abc123"
    assert result["area"] == "fragments"
    assert result["created_at"] == "2026-03-01T12:30:00+00:00"
    assert result["source_type"] == "memory"
    assert result["memory_type"] == "episodic"
    assert result["tags"] == ["tool", "python"]
    assert result["confidence"] == 0.5
    assert result["retrieved_count"] == 0


def test_doc_to_payload_generates_id():
    result = doc_to_payload("content", {})
    assert "memory_id" in result
    assert len(result["memory_id"]) == 10


def test_doc_to_payload_timestamp_conversion():
    metadata = {"timestamp": "2026-01-15 09:00:00"}
    result = doc_to_payload("text", metadata)
    assert result["created_at"] == "2026-01-15T09:00:00+00:00"


def test_doc_to_payload_passthrough_extra_metadata():
    metadata = {
        "id": "x",
        "timestamp": "2026-01-01 00:00:00",
        "custom_field": "custom_value",
        "another": 42,
    }
    result = doc_to_payload("text", metadata)
    assert result["custom_field"] == "custom_value"
    assert result["another"] == 42


def test_payload_to_doc_basic():
    payload = {
        "content": "some memory",
        "memory_id": "mem1",
        "area": "solutions",
        "created_at": "2026-03-01T12:30:00+00:00",
        "knowledge_source": True,
        "source_file": "test.py",
        "file_type": "python",
        "consolidation_action": "keep",
        "tags": ["a", "b"],
    }
    result = payload_to_doc(payload, score=0.95)
    assert result["page_content"] == "some memory"
    assert result["metadata"]["id"] == "mem1"
    assert result["metadata"]["area"] == "solutions"
    assert result["metadata"]["knowledge_source"] is True
    assert result["metadata"]["source_file"] == "test.py"
    assert result["metadata"]["file_type"] == "python"
    assert result["metadata"]["consolidation_action"] == "keep"
    assert result["metadata"]["tags"] == ["a", "b"]
    assert result["metadata"]["_score"] == 0.95


def test_payload_to_doc_timestamp_conversion():
    payload = {"created_at": "2026-06-15T14:30:00+00:00"}
    result = payload_to_doc(payload)
    assert result["metadata"]["timestamp"] == "2026-06-15 14:30:00"


def test_extract_area_filter_single_quotes():
    assert extract_area_filter("area == 'fragments'") == "fragments"


def test_extract_area_filter_double_quotes():
    assert extract_area_filter('area == "main"') == "main"


def test_extract_area_filter_no_match():
    assert extract_area_filter("foo == 'bar'") is None
    assert extract_area_filter("") is None
    assert extract_area_filter("area != 'main'") is None


def test_generate_id_length():
    generated = _generate_id()
    assert len(generated) == 10
    assert generated.isalnum()


def test_roundtrip():
    """doc_to_payload → payload_to_doc preserves content + key metadata."""
    original_content = "The agent learned something new"
    original_metadata = {
        "id": "rt_test_1",
        "timestamp": "2026-02-20 08:15:00",
        "area": "main",
        "tags": ["learning", "test"],
        "knowledge_source": True,
        "source_file": "conversation.txt",
        "file_type": "text",
    }
    payload = doc_to_payload(original_content, original_metadata)
    doc = payload_to_doc(payload)
    assert doc["page_content"] == original_content
    assert doc["metadata"]["id"] == "rt_test_1"
    assert doc["metadata"]["area"] == "main"
    assert doc["metadata"]["tags"] == ["learning", "test"]
    assert doc["metadata"]["knowledge_source"] is True
    assert doc["metadata"]["source_file"] == "conversation.txt"
    assert doc["metadata"]["file_type"] == "text"
    # Timestamp survives roundtrip
    assert doc["metadata"]["timestamp"] == "2026-02-20 08:15:00"


def test_memory_subdir_to_collection():
    assert memory_subdir_to_collection("default") == "episodic_memory"
    assert memory_subdir_to_collection("custom") == "episodic_memory"
