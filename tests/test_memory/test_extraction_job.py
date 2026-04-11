"""Tests for memory extraction job."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

    @pytest.mark.asyncio
    async def test_summary_includes_references_captured_key(self):
        """Summary dict must include references_captured for observability."""
        db = AsyncMock()
        cursor = AsyncMock()
        cursor.description = [
            ("id",), ("cc_session_id",), ("source_tag",),
            ("last_extracted_at",), ("last_extracted_line",), ("started_at",),
        ]
        cursor.fetchall = AsyncMock(return_value=[])
        db.execute = AsyncMock(return_value=cursor)

        summary = await run_extraction_cycle(
            db=db, store=AsyncMock(), router=AsyncMock(),
        )
        assert "references_captured" in summary
        assert summary["references_captured"] == 0

    @pytest.mark.asyncio
    async def test_reference_only_mode_skips_episodic_and_watermark(
        self, tmp_path,
    ):
        """reference_only_mode: skip episodic store.store calls + watermark updates."""
        import json as _json

        import aiosqlite

        from genesis.db.schema import create_all_tables

        # Build a minimal JSONL transcript
        jsonl = tmp_path / "mine-session.jsonl"
        msgs = [
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-04-11T12:00:00Z",
                "message": {"role": "user", "content": [
                    {"type": "text", "text": "login is 614Buckeye password is OhioState614!Bucks"},
                ]},
            },
        ]
        jsonl.write_text("\n".join(_json.dumps(m) for m in msgs) + "\n")

        async with aiosqlite.connect(":memory:") as real_db:
            await create_all_tables(real_db)
            # Seed a session row pointing at the test transcript
            await real_db.execute(
                "INSERT INTO cc_sessions "
                "(id, session_type, model, cc_session_id, source_tag, "
                "started_at, last_activity_at, status, last_extracted_line) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("s-mine", "foreground", "claude-sonnet-4-6", "mine-session",
                 "foreground", "2026-04-11T12:00:00+00:00",
                 "2026-04-11T12:00:00+00:00", "active", 999),  # high watermark
            )
            await real_db.commit()

            # Router returns a pre-built extraction result
            router = AsyncMock()
            router.route_call = AsyncMock(return_value=AsyncMock(
                success=True,
                content=(
                    '```json\n{"extractions": [{"content": "ScarletAndRage '
                    'login: username: 614Buckeye password: OhioState614!Bucks", '
                    '"type": "entity", "confidence": 0.9, '
                    '"entities": ["ScarletAndRage"]}]}\n```'
                ),
                call_site_id="9_fact_extraction",
                error=None,
            ))

            store = AsyncMock()
            store.store = AsyncMock(return_value="qdrant-ref")
            store.delete = AsyncMock()
            store._embeddings = MagicMock()
            store._embeddings.model_name = "m"

            summary = await run_extraction_cycle(
                db=real_db, store=store, router=router,
                transcript_dir=tmp_path,
                reference_only_mode=True,
                start_line_override=0,  # ignore watermark
            )

            # References were captured
            assert summary["references_captured"] >= 1
            # Episodic storage was NOT called (references don't count)
            assert summary["entities_extracted"] == 0
            # Watermark unchanged — reference_only_mode must not advance it
            cursor = await real_db.execute(
                "SELECT last_extracted_line FROM cc_sessions WHERE id='s-mine'",
            )
            assert (await cursor.fetchone())[0] == 999

            # Verify reference row was actually written
            cursor = await real_db.execute(
                "SELECT COUNT(*) FROM knowledge_units "
                "WHERE project_type='reference'",
            )
            assert (await cursor.fetchone())[0] >= 1
