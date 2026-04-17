"""Tests for knowledge distillation pipeline."""

import json
from unittest.mock import AsyncMock, MagicMock

from genesis.knowledge.distillation import (
    DistillationPipeline,
    KnowledgeUnit,
    _chunk_text,
    _parse_llm_response,
)
from genesis.knowledge.processors.base import ProcessedContent

# ─── _chunk_text ────────────────────────────────────────────────────────────


def test_chunk_text_short():
    """Short text should not be chunked."""
    chunks = _chunk_text("Short text", max_chars=100)
    assert len(chunks) == 1
    assert chunks[0] == "Short text"


def test_chunk_text_splits_on_paragraphs():
    text = "Para one.\n\nPara two.\n\nPara three."
    chunks = _chunk_text(text, max_chars=20)
    assert len(chunks) >= 2
    assert "Para one." in chunks[0]


# ─── _parse_llm_response ───────────────────────────────────────────────────


def test_parse_json_array():
    response = '[{"concept": "VPC", "body": "Virtual Private Cloud"}]'
    result = _parse_llm_response(response)
    assert len(result) == 1
    assert result[0]["concept"] == "VPC"


def test_parse_json_with_markdown_fences():
    response = '```json\n[{"concept": "S3", "body": "Object storage"}]\n```'
    result = _parse_llm_response(response)
    assert len(result) == 1
    assert result[0]["concept"] == "S3"


def test_parse_single_object():
    response = '{"concept": "IAM", "body": "Identity management"}'
    result = _parse_llm_response(response)
    assert len(result) == 1


def test_parse_invalid_json():
    result = _parse_llm_response("not json at all")
    assert result == []


def test_parse_embedded_array():
    response = 'Here are the results: [{"concept": "Test"}] done.'
    result = _parse_llm_response(response)
    assert len(result) == 1


# ─── DistillationPipeline ──────────────────────────────────────────────────


async def test_distill_happy_path():
    """Distillation produces KnowledgeUnits from ProcessedContent."""
    mock_router = MagicMock()
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.content = json.dumps([
        {
            "concept": "VPC Fundamentals",
            "body": "A VPC provides network isolation in the cloud.",
            "domain": "aws",
            "relationships": ["subnets", "security-groups"],
            "caveats": ["simplified"],
            "tags": ["networking", "cloud"],
            "confidence": 0.9,
        },
    ])
    mock_router.route_call = AsyncMock(return_value=mock_result)

    pipeline = DistillationPipeline(router=mock_router)
    content = ProcessedContent(
        text="VPC is a virtual private cloud that provides network isolation.",
        source_type="text",
        source_path="/tmp/test.txt",
    )

    units = await pipeline.distill(content, project_type="cloud-eng", domain="aws")

    assert len(units) == 1
    assert isinstance(units[0], KnowledgeUnit)
    assert units[0].concept == "VPC Fundamentals"
    assert units[0].confidence == 0.9
    assert "networking" in units[0].tags


async def test_distill_filters_low_confidence():
    """Units below confidence threshold are skipped."""
    mock_router = MagicMock()
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.content = json.dumps([
        {"concept": "Good", "body": "High quality.", "domain": "test", "confidence": 0.8},
        {"concept": "Bad", "body": "Low quality.", "domain": "test", "confidence": 0.2},
    ])
    mock_router.route_call = AsyncMock(return_value=mock_result)

    pipeline = DistillationPipeline(router=mock_router)
    content = ProcessedContent(text="Some content", source_type="text", source_path="test.txt")

    units = await pipeline.distill(content, project_type="test")
    assert len(units) == 1
    assert units[0].concept == "Good"


async def test_distill_empty_content():
    """Empty content returns no units."""
    mock_router = MagicMock()
    pipeline = DistillationPipeline(router=mock_router)
    content = ProcessedContent(text="", source_type="text", source_path="empty.txt")

    units = await pipeline.distill(content, project_type="test")
    assert units == []


async def test_distill_llm_failure():
    """LLM failure returns empty list, doesn't raise."""
    mock_router = MagicMock()
    mock_result = MagicMock()
    mock_result.success = False
    mock_result.content = None
    mock_result.error = "Provider unavailable"
    mock_router.route_call = AsyncMock(return_value=mock_result)

    pipeline = DistillationPipeline(router=mock_router)
    content = ProcessedContent(text="Some content", source_type="text", source_path="test.txt")

    units = await pipeline.distill(content, project_type="test")
    assert units == []
