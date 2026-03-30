"""Tests for CodeAuditExecutor and CodebaseContextGatherer."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.surplus.code_audit import CodeAuditExecutor, CodebaseContextGatherer, _is_slop
from genesis.surplus.types import (
    ComputeTier,
    ExecutorResult,
    SurplusTask,
    TaskStatus,
    TaskType,
)


def _make_task(task_type: TaskType = TaskType.CODE_AUDIT) -> SurplusTask:
    return SurplusTask(
        id="task-1",
        task_type=task_type,
        compute_tier=ComputeTier.FREE_API,
        priority=0.5,
        drive_alignment="competence",
        status=TaskStatus.RUNNING,
        created_at="2026-03-18T00:00:00",
    )


# ---------------------------------------------------------------------------
# CodebaseContextGatherer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gatherer_assembles_context():
    """Gatherer runs subprocess commands and assembles sections."""
    db = AsyncMock()
    cursor_mock = AsyncMock()
    cursor_mock.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=cursor_mock)

    gatherer = CodebaseContextGatherer(db, repo_root="/tmp/fake")

    with patch.object(gatherer, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            "abc123 some commit\ndef456 another",  # git log
            " file1.py | 5 ++\n file2.py | 3 --",  # git diff
            "src/genesis/foo.py\nsrc/genesis/bar.py",  # find
        ]
        ctx = await gatherer.gather()

    assert "Recent Commits" in ctx
    assert "abc123" in ctx
    assert "Source Files" in ctx
    assert "foo.py" in ctx


@pytest.mark.asyncio
async def test_gatherer_caps_context_length():
    """Context is truncated to ~4000 chars."""
    db = AsyncMock()
    cursor_mock = AsyncMock()
    cursor_mock.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=cursor_mock)

    gatherer = CodebaseContextGatherer(db, repo_root="/tmp/fake")

    with patch.object(gatherer, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            "x" * 2000,
            "y" * 2000,
            "z" * 2000,
        ]
        ctx = await gatherer.gather()

    assert len(ctx) <= 4100  # 4000 + truncation notice
    assert "truncated" in ctx


@pytest.mark.asyncio
async def test_gatherer_includes_previous_findings():
    """Previous unresolved findings from DB are included."""
    db = AsyncMock()
    cursor_mock = AsyncMock()
    cursor_mock.fetchall = AsyncMock(return_value=[
        ("Missing error handling in router.py",),
        ("Unused import in types.py",),
    ])
    db.execute = AsyncMock(return_value=cursor_mock)

    gatherer = CodebaseContextGatherer(db, repo_root="/tmp/fake")

    with patch.object(gatherer, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = ["log", "diff", "files"]
        ctx = await gatherer.gather()

    assert "Previous Unresolved Findings" in ctx
    assert "Missing error handling" in ctx
    assert "Unused import" in ctx


@pytest.mark.asyncio
async def test_gatherer_handles_subprocess_timeout():
    """Gatherer returns empty string for timed-out subprocesses."""
    db = AsyncMock()
    cursor_mock = AsyncMock()
    cursor_mock.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=cursor_mock)

    gatherer = CodebaseContextGatherer(db, repo_root="/tmp/fake")

    # _run_cmd handles timeout internally, returns ""
    with patch.object(gatherer, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = ["", "", ""]
        ctx = await gatherer.gather()

    assert "Recent Commits" in ctx
    assert "(none)" in ctx


# ---------------------------------------------------------------------------
# CodeAuditExecutor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_wrong_task_type():
    """Executor rejects non-CODE_AUDIT tasks."""
    router = MagicMock()
    db = AsyncMock()
    executor = CodeAuditExecutor(router=router, db=db)

    task = _make_task(TaskType.BRAINSTORM_SELF)
    result = await executor.execute(task)

    assert not result.success
    assert "Wrong task type" in (result.error or "")


@pytest.mark.asyncio
async def test_executor_successful_json_parse():
    """Executor parses valid JSON findings from router response."""
    findings_json = json.dumps([
        {"file": "foo.py", "line": 10, "severity": "high",
         "suggestion": "Fix this", "confidence": 0.9},
    ])

    routing_result = MagicMock()
    routing_result.success = True
    routing_result.content = findings_json
    routing_result.provider_used = "openrouter"
    routing_result.error = None

    router = AsyncMock()
    router.route_call = AsyncMock(return_value=routing_result)

    db = AsyncMock()
    cursor_mock = AsyncMock()
    cursor_mock.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=cursor_mock)

    executor = CodeAuditExecutor(router=router, db=db, repo_root="/tmp/fake")

    with patch.object(executor._gatherer, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = ["log", "diff", "files"]
        result = await executor.execute(_make_task())

    assert result.success
    assert len(result.insights) == 1
    assert result.insights[0]["file"] == "foo.py"
    assert result.insights[0]["severity"] == "high"
    assert result.insights[0]["confidence"] == 0.9
    assert result.insights[0]["generating_model"] == "openrouter"
    assert result.insights[0]["source_task_type"] == "code_audit"


@pytest.mark.asyncio
async def test_executor_malformed_json_fallback():
    """Executor falls back gracefully on malformed JSON."""
    routing_result = MagicMock()
    routing_result.success = True
    routing_result.content = "Here are some issues I found: blah blah"
    routing_result.provider_used = "groq"
    routing_result.error = None

    router = AsyncMock()
    router.route_call = AsyncMock(return_value=routing_result)

    db = AsyncMock()
    cursor_mock = AsyncMock()
    cursor_mock.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=cursor_mock)

    executor = CodeAuditExecutor(router=router, db=db, repo_root="/tmp/fake")

    with patch.object(executor._gatherer, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = ["log", "diff", "files"]
        result = await executor.execute(_make_task())

    assert result.success
    assert len(result.insights) == 1
    assert result.insights[0]["confidence"] == 0.15  # low confidence fallback
    assert "blah blah" in result.insights[0]["content"]


@pytest.mark.asyncio
async def test_executor_json_in_code_block():
    """Executor extracts JSON from markdown code blocks."""
    content = '```json\n[{"file": "x.py", "severity": "medium", "suggestion": "refactor", "confidence": 0.7}]\n```'

    routing_result = MagicMock()
    routing_result.success = True
    routing_result.content = content
    routing_result.provider_used = "mistral"
    routing_result.error = None

    router = AsyncMock()
    router.route_call = AsyncMock(return_value=routing_result)

    db = AsyncMock()
    cursor_mock = AsyncMock()
    cursor_mock.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=cursor_mock)

    executor = CodeAuditExecutor(router=router, db=db, repo_root="/tmp/fake")

    with patch.object(executor._gatherer, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = ["log", "diff", "files"]
        result = await executor.execute(_make_task())

    assert result.success
    assert len(result.insights) == 1
    assert result.insights[0]["file"] == "x.py"


@pytest.mark.asyncio
async def test_executor_routing_failure():
    """Executor returns failure when router fails."""
    routing_result = MagicMock()
    routing_result.success = False
    routing_result.error = "all providers down"
    routing_result.content = None

    router = AsyncMock()
    router.route_call = AsyncMock(return_value=routing_result)

    db = AsyncMock()
    cursor_mock = AsyncMock()
    cursor_mock.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=cursor_mock)

    executor = CodeAuditExecutor(router=router, db=db, repo_root="/tmp/fake")

    with patch.object(executor._gatherer, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = ["log", "diff", "files"]
        result = await executor.execute(_make_task())

    assert not result.success
    assert "all providers down" in (result.error or "")


@pytest.mark.asyncio
async def test_executor_returns_proper_executor_result():
    """Verify return type is ExecutorResult with correct structure."""
    findings_json = json.dumps([
        {"file": "a.py", "severity": "low", "suggestion": "nit", "confidence": 0.4},
        {"file": "b.py", "severity": "high", "suggestion": "critical", "confidence": 0.95},
    ])

    routing_result = MagicMock()
    routing_result.success = True
    routing_result.content = findings_json
    routing_result.provider_used = "openrouter"
    routing_result.error = None

    router = AsyncMock()
    router.route_call = AsyncMock(return_value=routing_result)

    db = AsyncMock()
    cursor_mock = AsyncMock()
    cursor_mock.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=cursor_mock)

    executor = CodeAuditExecutor(router=router, db=db, repo_root="/tmp/fake")

    with patch.object(executor._gatherer, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = ["log", "diff", "files"]
        result = await executor.execute(_make_task())

    assert isinstance(result, ExecutorResult)
    assert result.success is True
    assert len(result.insights) == 2
    for insight in result.insights:
        assert "content" in insight
        assert "source_task_type" in insight
        assert "generating_model" in insight
        assert "drive_alignment" in insight
        assert "confidence" in insight
        assert "file" in insight
        assert "severity" in insight
        assert "suggestion" in insight


# ---------------------------------------------------------------------------
# Slop / quality filter
# ---------------------------------------------------------------------------


class TestSlopFilter:
    def test_slop_prefixes_detected(self):
        assert _is_slop("Great work on this codebase!")
        assert _is_slop("The code looks clean and well-structured")
        assert _is_slop("No issues found in the codebase")
        assert _is_slop("  Overall, the codebase is solid  ")
        assert _is_slop("Looks good overall")
        assert _is_slop("Well-structured codebase with clear patterns")

    def test_legitimate_findings_pass(self):
        assert not _is_slop("Missing error handling in router.py line 42")
        assert not _is_slop("Bare except clause swallows TypeError")
        assert not _is_slop("SQL injection risk in user_input parameter")
        assert not _is_slop("Unused import: asyncio in types.py")

    def test_empty_is_not_slop(self):
        # Empty strings are filtered separately (before slop check)
        assert not _is_slop("")
        assert not _is_slop("   ")


def test_parse_findings_filters_slop():
    """Slop suggestions are dropped from parsed findings."""
    findings_json = json.dumps([
        {"file": "a.py", "severity": "high", "suggestion": "Fix bare except", "confidence": 0.9},
        {"file": "b.py", "severity": "low", "suggestion": "The code looks great overall", "confidence": 0.8},
        {"file": "c.py", "severity": "low", "suggestion": "", "confidence": 0.5},
    ])
    results = CodeAuditExecutor._parse_findings(
        findings_json, provider="test", drive_alignment="competence",
    )
    # Only the first finding should survive (slop and empty filtered)
    assert len(results) == 1
    assert results[0]["file"] == "a.py"
    assert results[0]["suggestion"] == "Fix bare except"


def test_parse_findings_all_slop_returns_no_findings():
    """If all findings are slop, return the 'No findings' fallback."""
    findings_json = json.dumps([
        {"suggestion": "The code looks clean", "confidence": 0.8},
        {"suggestion": "No issues found", "confidence": 0.9},
    ])
    results = CodeAuditExecutor._parse_findings(
        findings_json, provider="test", drive_alignment="competence",
    )
    assert len(results) == 1
    assert results[0]["suggestion"] == "No findings"
    assert results[0]["confidence"] == 0.15
