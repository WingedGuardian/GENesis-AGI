"""Tests for PostExecutionAuditor — transcript parsing and autonomy feedback."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.autonomy.audit import PostExecutionAuditor


def _make_transcript(tool_calls: list[dict]) -> str:
    """Create a temporary .jsonl transcript with tool_use entries."""
    lines = []
    for tc in tool_calls:
        entry = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": tc["name"],
                        "input": tc.get("input", {}),
                    }
                ]
            },
        }
        lines.append(json.dumps(entry))
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as tmp:
        tmp.write("\n".join(lines))
    return tmp.name


def _make_auditor(**kwargs) -> PostExecutionAuditor:
    mgr = MagicMock()
    mgr.record_success = AsyncMock(return_value=(True, False))
    mgr.record_correction = AsyncMock(return_value=(True, False))
    return PostExecutionAuditor(
        autonomy_manager=mgr,
        **kwargs,
    )


class TestTranscriptParsing:
    """Test file path extraction from .jsonl transcripts."""

    @pytest.mark.asyncio
    async def test_extracts_write_paths(self) -> None:
        transcript = _make_transcript([
            {"name": "Write", "input": {"file_path": "/home/test/output.md", "content": "hello"}},
            {"name": "Read", "input": {"file_path": "/home/test/input.md"}},
            {"name": "Edit", "input": {"file_path": "/home/test/config.py", "old_string": "a", "new_string": "b"}},
        ])
        auditor = _make_auditor()
        result = await auditor.audit_session(
            "test-session",
            transcript_path=transcript,
            tools_summary={"Write": 1, "Read": 1, "Edit": 1},
            session_success=True,
        )
        assert result.success
        assert len(result.files_touched) == 2  # Write + Edit, not Read
        assert "/home/test/output.md" in result.files_touched
        assert "/home/test/config.py" in result.files_touched
        Path(transcript).unlink()

    @pytest.mark.asyncio
    async def test_skips_parsing_without_write_tools(self) -> None:
        """If tools_summary has no Write/Edit, skip transcript parsing."""
        auditor = _make_auditor()
        result = await auditor.audit_session(
            "test-session",
            transcript_path="/nonexistent/path.jsonl",  # would fail if parsed
            tools_summary={"Read": 5, "Grep": 3},
            session_success=True,
        )
        assert result.success
        assert result.files_touched == []
        auditor._autonomy_manager.record_success.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_transcript_no_crash(self) -> None:
        auditor = _make_auditor()
        result = await auditor.audit_session(
            "test-session",
            transcript_path="/nonexistent/path.jsonl",
            tools_summary={"Write": 1},
            session_success=True,
        )
        assert result.success  # No transcript → no violations → success
        assert result.files_touched == []


class TestProtectedPathViolations:
    """Test detection of protected path violations."""

    @pytest.mark.asyncio
    async def test_critical_path_violation(self) -> None:
        from genesis.autonomy.protection import ProtectedPathRegistry

        protected = ProtectedPathRegistry.from_yaml()
        auditor = PostExecutionAuditor(
            protected_paths=protected,
            autonomy_manager=MagicMock(
                record_correction=AsyncMock(return_value=(True, False)),
                record_success=AsyncMock(return_value=(True, False)),
            ),
        )
        transcript = _make_transcript([
            {"name": "Write", "input": {"file_path": "src/genesis/channels/telegram/adapter.py", "content": "hack"}},
        ])
        result = await auditor.audit_session(
            "test-session",
            transcript_path=transcript,
            tools_summary={"Write": 1},
            session_success=True,
        )
        assert not result.success
        assert len(result.violations) > 0
        assert "critical" in result.violations[0].lower()
        auditor._autonomy_manager.record_correction.assert_awaited_once()
        Path(transcript).unlink()


class TestAutonomyFeedback:
    """Test that success/correction signals reach AutonomyManager."""

    @pytest.mark.asyncio
    async def test_success_feeds_manager(self) -> None:
        auditor = _make_auditor()
        await auditor.audit_session(
            "test-session",
            tools_summary={"Read": 1},
            session_success=True,
        )
        auditor._autonomy_manager.record_success.assert_awaited_once_with(
            "background_cognitive",
        )

    @pytest.mark.asyncio
    async def test_failure_feeds_correction(self) -> None:
        auditor = _make_auditor()
        await auditor.audit_session(
            "test-session",
            tools_summary={"Read": 1},
            session_success=False,
        )
        auditor._autonomy_manager.record_correction.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_manager_no_crash(self) -> None:
        auditor = PostExecutionAuditor()
        result = await auditor.audit_session(
            "test-session",
            tools_summary={"Read": 1},
            session_success=True,
        )
        assert result.success


class TestAnUnauditableSessionFailsClosed:
    """A session whose stream was incomplete AND whose transcript is missing
    has produced NO evidence. It must not be certified clean.

    The route is specific and was reachable in production: an ego direct
    session drops a tool event (so the caller withholds `tools_summary`) and
    also exits through the background-truncated fallback, which returns
    `session_id=""` and therefore persists an empty `transcript_path`. With no
    summary the pre-filter is skipped, with no transcript `files_touched` is
    empty, no violations are found, and a successful session takes the
    clean-pass branch — so an oversized protected-path Write is recorded as a
    pass over nothing at all (Codex P1, PR #1625).
    """

    @pytest.mark.asyncio
    async def test_no_summary_and_no_transcript_is_a_correction(self) -> None:
        auditor = _make_auditor()
        result = await auditor.audit_session(
            "test-session",
            transcript_path="",
            tools_summary=None,
            session_success=True,
            stream_incomplete=True,
        )
        assert result.success is False, (
            "an incomplete stream with no transcript was certified clean — "
            "this is the fail-open the PR closes"
        )
        auditor._autonomy_manager.record_correction.assert_awaited_once()
        auditor._autonomy_manager.record_success.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_named_but_absent_transcript_is_also_unauditable(self) -> None:
        """An empty path and a path to a file that does not exist are the same
        thing: no evidence. `_parse_transcript` treats a missing file as "no
        files touched", which reads identically to "touched nothing"."""
        auditor = _make_auditor()
        result = await auditor.audit_session(
            "test-session",
            transcript_path="/nonexistent/transcript-that-is-not-there.jsonl",
            tools_summary=None,
            session_success=True,
            stream_incomplete=True,
        )
        assert result.success is False
        auditor._autonomy_manager.record_correction.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_complete_stream_with_no_transcript_still_passes(self) -> None:
        """CONTROL, and load-bearing: without it, failing EVERY session that
        lacks a transcript would satisfy the tests above while turning a
        normal no-op run into a correction and degrading autonomy for nothing.
        """
        auditor = _make_auditor()
        result = await auditor.audit_session(
            "test-session",
            transcript_path="",
            tools_summary={"Read": 1},
            session_success=True,
            stream_incomplete=False,
        )
        assert result.success is True
        auditor._autonomy_manager.record_success.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_incomplete_stream_with_a_readable_transcript_is_audited(self) -> None:
        """The transcript is the fallback evidence, so when it EXISTS an
        incomplete stream must still be audited on its contents rather than
        short-circuited."""
        transcript = _make_transcript([
            {"name": "Write", "input": {"file_path": "/tmp/harmless.txt"}},
        ])
        auditor = _make_auditor()
        result = await auditor.audit_session(
            "test-session",
            transcript_path=transcript,
            tools_summary=None,
            session_success=True,
            stream_incomplete=True,
        )
        assert result.success is True, (
            "a readable transcript was ignored, so the fallback evidence path "
            "is dead and every incomplete stream now fails"
        )
        assert "/tmp/harmless.txt" in result.files_touched
