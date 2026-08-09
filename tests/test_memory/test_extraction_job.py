"""Tests for memory extraction job."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
        mock_sessions = [
            {"id": "s1", "cc_session_id": "cc1", "source_tag": "foreground",
             "last_extracted_at": None, "last_extracted_line": 0, "started_at": "2026-03-23"},
            {"id": "s2", "cc_session_id": "cc2", "source_tag": "voice",
             "last_extracted_at": None, "last_extracted_line": 0, "started_at": "2026-03-23"},
        ]
        with patch("genesis.db.crud.cc_sessions.get_extractable",
                    new_callable=AsyncMock, return_value=mock_sessions) as mock_get:
            sessions = await _find_extractable_sessions(db)
            assert len(sessions) == 2
            assert sessions[0]["source_tag"] == "foreground"
            assert sessions[1]["source_tag"] == "voice"
            mock_get.assert_called_once()


class TestSourceTagCoverage:
    """The tag taxonomy must classify every production source_tag family —
    the guard that would have caught the inbox_evaluation phantom-tag bug."""

    def test_extractable_and_excluded_disjoint(self):
        from genesis.memory.extraction_job import (
            _EXCLUDED_SOURCE_TAGS,
            _EXTRACTABLE_SOURCE_TAGS,
        )

        assert not (_EXTRACTABLE_SOURCE_TAGS & _EXCLUDED_SOURCE_TAGS)

    def test_inbox_evaluation_is_deliberately_excluded(self):
        """The real inbox tag is inbox_evaluation (not 'inbox'), and it is an
        intentional exclusion — extracted via the curated-output path instead."""
        from genesis.memory.extraction_job import (
            _EXCLUDED_SOURCE_TAGS,
            _EXTRACTABLE_SOURCE_TAGS,
        )

        assert "inbox_evaluation" in _EXCLUDED_SOURCE_TAGS
        assert "inbox_evaluation" not in _EXTRACTABLE_SOURCE_TAGS
        assert "inbox" not in _EXTRACTABLE_SOURCE_TAGS  # the phantom is gone

    def test_source_tag_coverage_guard(self):
        """Every ``source_tag=`` keyword literal in src/ is consciously
        classified as extractable or excluded — no silent holes. Derives its
        known set by scanning src/ (not a hand-maintained snapshot, which is
        exactly how inbox_evaluation and mail_reply slipped).

        Scope/limit: the regex sees the ``source_tag="..."`` keyword-argument
        idiom — the shape EVERY live dispatch uses (session_manager /
        DirectSessionRequest). A source_tag written via raw parameterized SQL
        (positional bind, e.g. resilience/cc_budget.py) is invisible to the
        scan and must be classified by hand in _EXCLUDED_SOURCE_TAG_PREFIXES;
        the known indirect writers are asserted explicitly below."""
        import re

        from genesis.env import repo_root
        from genesis.memory.extraction_job import (
            _EXCLUDED_SOURCE_TAG_PREFIXES,
            _EXCLUDED_SOURCE_TAGS,
            _EXTRACTABLE_SOURCE_TAGS,
        )

        # Captures the static part of a source_tag string literal; group 2 marks
        # an f-string interpolation (e.g. "reflection_{...}", "user_job:{...}"),
        # which is treated as a PREFIX family. Only matches assignment (a quote
        # after =), never == comparisons.
        pat = re.compile(r"""source_tag\s*=\s*f?["']([^"'{]*)(\{)?""")
        literals: set[tuple[str, bool]] = set()
        for py in (repo_root() / "src").rglob("*.py"):
            for line in py.read_text(encoding="utf-8", errors="ignore").splitlines():
                # Drop comments so prose mentioning source_tag="..." (e.g. this
                # module's own docstrings) can't inject phantom literals.
                code = line.split("#", 1)[0]
                for m in pat.finditer(code):
                    static, is_prefix = m.group(1), bool(m.group(2))
                    if static:
                        literals.add((static, is_prefix))

        prefixes = set(_EXCLUDED_SOURCE_TAG_PREFIXES)

        def classified(static: str, is_prefix: bool) -> bool:
            if is_prefix:  # f-string stem — must be a registered excluded prefix
                return static.rstrip("_:-") in prefixes
            return (
                static in _EXTRACTABLE_SOURCE_TAGS
                or static in _EXCLUDED_SOURCE_TAGS
                or static.startswith(_EXCLUDED_SOURCE_TAG_PREFIXES)
            )

        assert literals, "no source_tag literals found — regex or repo_root broken"
        unclassified = sorted(
            s for s, is_prefix in literals if not classified(s, is_prefix)
        )
        assert not unclassified, f"unclassified source_tag literals: {unclassified}"

        # Indirect writers the regex cannot see (raw parameterized SQL) must be
        # classified by hand — assert the known ones so they can't silently drop.
        known_indirect = ["priority_0"]  # cc_budget.py f"priority_{n}" INSERT
        missing = [t for t in known_indirect if not classified(t, False)]
        assert not missing, f"unclassified indirect source_tags: {missing}"

class TestUpdateWatermark:
    """Tests for _update_watermark."""

    @pytest.mark.asyncio
    async def test_updates_watermark(self):
        db = AsyncMock()
        with patch("genesis.db.crud.cc_sessions.update_extraction_watermark",
                    new_callable=AsyncMock, return_value=True) as mock_update:
            await _update_watermark(db, "session-1", 150)
            mock_update.assert_called_once()
            call_kwargs = mock_update.call_args
            assert call_kwargs[1]["last_extracted_line"] == 150


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
                    {"type": "text", "text": "login is ForumUser42 password is Passw0rd!x9z"},
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
                    '```json\n{"extractions": [{"content": "HobbyForum '
                    'login: username: ForumUser42 password: Passw0rd!x9z", '
                    '"type": "entity", "confidence": 0.9, '
                    '"entities": ["HobbyForum"]}]}\n```'
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
