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


# ─── New tests: chunk size, doc stats, extraction ratio ────────────────────


def test_max_chunk_chars_default():
    """Default chunk size should be 40K (upgraded from 12K)."""
    from genesis.knowledge.distillation import _MAX_CHUNK_CHARS
    assert _MAX_CHUNK_CHARS == 40_000


def test_min_extraction_ratio_default():
    """Minimum extraction ratio should be 10%."""
    from genesis.knowledge.distillation import _MIN_EXTRACTION_RATIO
    assert _MIN_EXTRACTION_RATIO == 0.10


async def test_distill_passes_doc_stats():
    """LLM user message should include document scale information."""
    mock_router = MagicMock()
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.content = json.dumps([
        {
            "concept": "Test Concept",
            "body": "Test body content.",
            "domain": "test",
            "confidence": 0.9,
        },
    ])
    mock_router.route_call = AsyncMock(return_value=mock_result)

    pipeline = DistillationPipeline(router=mock_router)
    content = ProcessedContent(
        text="A" * 500,
        source_type="text",
        source_path="/test.txt",
    )

    await pipeline.distill(content, project_type="test", domain="test")

    # Verify route_call was called and user message contains doc stats
    assert mock_router.route_call.called
    call_args = mock_router.route_call.call_args
    messages = call_args[0][1]  # Second positional arg
    user_msg = messages[1]["content"]
    assert "500 characters total" in user_msg
    assert "chunk 1 of 1" in user_msg


async def test_distill_passes_page_count():
    """LLM user message should include page count from metadata."""
    mock_router = MagicMock()
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.content = json.dumps([
        {"concept": "Test", "body": "Body.", "domain": "test", "confidence": 0.9},
    ])
    mock_router.route_call = AsyncMock(return_value=mock_result)

    pipeline = DistillationPipeline(router=mock_router)
    content = ProcessedContent(
        text="Some PDF content here.",
        source_type="pdf",
        source_path="/test.pdf",
        metadata={"page_count": 42},
    )

    await pipeline.distill(content, project_type="test", domain="test")

    user_msg = mock_router.route_call.call_args[0][1][1]["content"]
    assert "42 pages" in user_msg


async def test_extraction_ratio_tracked():
    """Pipeline should track extraction ratio after distillation."""
    mock_router = MagicMock()
    mock_result = MagicMock()
    mock_result.success = True
    # 100 chars input, body is ~20 chars → ~20% ratio
    mock_result.content = json.dumps([
        {"concept": "Test", "body": "A" * 20, "domain": "test", "confidence": 0.9},
    ])
    mock_router.route_call = AsyncMock(return_value=mock_result)

    pipeline = DistillationPipeline(router=mock_router)
    content = ProcessedContent(
        text="B" * 100,
        source_type="text",
        source_path="/test.txt",
    )

    units = await pipeline.distill(content, project_type="test")
    assert len(units) == 1
    assert pipeline._last_extraction_ratio > 0
    assert abs(pipeline._last_extraction_ratio - 0.20) < 0.01


async def test_extraction_ratio_zero_on_empty():
    """Extraction ratio should be 0 when no content."""
    mock_router = MagicMock()
    pipeline = DistillationPipeline(router=mock_router)
    content = ProcessedContent(text="   ", source_type="text", source_path="empty.txt")

    await pipeline.distill(content, project_type="test")
    assert pipeline._last_extraction_ratio == 0.0
