"""Tests for memory extraction job."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from genesis.memory.extraction_job import (
    _extract_chunk,
    _find_extractable_sessions,
    _find_transcript,
    _update_watermark,
    run_extraction_cycle,
)
from genesis.util.jsonl import ConversationMessage


@dataclass(frozen=True)
class FakeRoutingResult:
    success: bool
    call_site_id: str = "9_fact_extraction"
    content: str | None = None
    error: str | None = None


def _make_jsonl(messages: list[dict], path: Path) -> Path:
    """Write a minimal JSONL transcript file."""
    jsonl_path = path / "test-session.jsonl"
    with open(jsonl_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")
    return jsonl_path


class TestExtractChunk:
    """Tests for _extract_chunk."""

    @pytest.mark.asyncio
    async def test_successful_extraction(self):
        router = AsyncMock()
        router.route_call.return_value = FakeRoutingResult(
            success=True,
            content='```json\n[{"content": "test entity", "type": "entity", "confidence": 0.8}]\n```',
        )
        chunk = [
            ConversationMessage(role="user", text="Hello", line_number=1),
            ConversationMessage(role="assistant", text="Hi there", line_number=2),
        ]
        result = await _extract_chunk(chunk=chunk, router=router)
        assert len(result.extractions) == 1
        assert result.extractions[0].content == "test entity"
        assert result.parse_error is None

    @pytest.mark.asyncio
    async def test_router_failure(self):
        router = AsyncMock()
        router.route_call.return_value = FakeRoutingResult(
            success=False,
            error="Provider chain exhausted",
        )
        chunk = [ConversationMessage(role="user", text="Hello", line_number=1)]
        result = await _extract_chunk(chunk=chunk, router=router)
        assert result.extractions == []
        assert "Provider chain exhausted" in result.parse_error

    @pytest.mark.asyncio
    async def test_parse_failure_retries(self):
        router = AsyncMock()
        # First call returns bad JSON, second returns good
        router.route_call.side_effect = [
            FakeRoutingResult(success=True, content="not json at all"),
            FakeRoutingResult(
                success=True,
                content='```json\n[{"content": "found it", "type": "entity", "confidence": 0.7}]\n```',
            ),
        ]
        chunk = [ConversationMessage(role="user", text="Hello", line_number=1)]
        result = await _extract_chunk(chunk=chunk, router=router, max_retries=2)
        assert len(result.extractions) == 1
        assert result.extractions[0].content == "found it"

    @pytest.mark.asyncio
    async def test_all_retries_exhausted(self):
        router = AsyncMock()
        router.route_call.return_value = FakeRoutingResult(
            success=True, content="not valid json",
        )
        chunk = [ConversationMessage(role="user", text="Hello", line_number=1)]
        result = await _extract_chunk(chunk=chunk, router=router, max_retries=2)
        assert result.extractions == []
        assert result.parse_error is not None

    @pytest.mark.asyncio
    async def test_router_exception(self):
        router = AsyncMock()
        router.route_call.side_effect = ConnectionError("network down")
        chunk = [ConversationMessage(role="user", text="Hello", line_number=1)]
        result = await _extract_chunk(chunk=chunk, router=router)
        assert result.extractions == []
        assert "network down" in result.parse_error


class TestFindTranscript:
    """Tests for _find_transcript."""

    def test_direct_file(self, tmp_path):
        jsonl = tmp_path / "abc123.jsonl"
        jsonl.write_text("{}\n")
        result = _find_transcript(tmp_path, "abc123")
        assert result == jsonl

    def test_subdirectory(self, tmp_path):
        subdir = tmp_path / "abc123"
        subdir.mkdir()
        jsonl = subdir / "abc123.jsonl"
        jsonl.write_text("{}\n")
        result = _find_transcript(tmp_path, "abc123")
        assert result == jsonl

    def test_not_found(self, tmp_path):
        result = _find_transcript(tmp_path, "nonexistent")
        assert result is None


class TestFindExtractableSessions:
    """Tests for _find_extractable_sessions."""

    @pytest.mark.asyncio
    async def test_filters_by_source_tag(self):
        db = AsyncMock()
        cursor = AsyncMock()
        cursor.description = [
            ("id",), ("cc_session_id",), ("source_tag",),
            ("last_extracted_at",), ("last_extracted_line",), ("started_at",),
        ]
        cursor.fetchall = AsyncMock(return_value=[
            ("s1", "cc1", "foreground", None, 0, "2026-03-23"),
            ("s2", "cc2", "inbox", None, 0, "2026-03-23"),
        ])
        db.execute = AsyncMock(return_value=cursor)

        sessions = await _find_extractable_sessions(db)
        assert len(sessions) == 2
        assert sessions[0]["source_tag"] == "foreground"
        assert sessions[1]["source_tag"] == "inbox"

        # Verify SQL includes proper filter
        sql = db.execute.call_args[0][0]
        assert "source_tag IN" in sql
        assert "active" in sql


class TestUpdateWatermark:
    """Tests for _update_watermark."""

    @pytest.mark.asyncio
    async def test_updates_watermark(self):
        db = AsyncMock()
        await _update_watermark(db, "session-1", 150)
        db.execute.assert_called_once()
        sql = db.execute.call_args[0][0]
        assert "last_extracted_at" in sql
        assert "last_extracted_line" in sql
        db.commit.assert_called_once()


class TestRunExtractionCycle:
    """Tests for run_extraction_cycle."""

    @pytest.mark.asyncio
    async def test_empty_sessions(self):
        db = AsyncMock()
        cursor = AsyncMock()
        cursor.description = [
            ("id",), ("cc_session_id",), ("source_tag",),
            ("last_extracted_at",), ("last_extracted_line",), ("started_at",),
        ]
        cursor.fetchall = AsyncMock(return_value=[])
        db.execute = AsyncMock(return_value=cursor)

        store = AsyncMock()
        router = AsyncMock()

        summary = await run_extraction_cycle(
            db=db, store=store, router=router,
        )
        assert summary["sessions_processed"] == 0
        assert summary["entities_extracted"] == 0

    @pytest.mark.asyncio
    async def test_session_with_no_transcript(self, tmp_path):
        db = AsyncMock()
        cursor = AsyncMock()
        cursor.description = [
            ("id",), ("cc_session_id",), ("source_tag",),
            ("last_extracted_at",), ("last_extracted_line",), ("started_at",),
        ]
        cursor.fetchall = AsyncMock(return_value=[
            ("s1", "nonexistent-session", "foreground", None, 0, "2026-03-23"),
        ])
        db.execute = AsyncMock(return_value=cursor)

        store = AsyncMock()
        router = AsyncMock()

        summary = await run_extraction_cycle(
            db=db, store=store, router=router,
            transcript_dir=tmp_path,
        )
        # Session skipped because no transcript found
        assert summary["sessions_processed"] == 0
