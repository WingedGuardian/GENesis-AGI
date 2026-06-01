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
