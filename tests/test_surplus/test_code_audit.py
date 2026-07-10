"""Tests for CodeAuditExecutor and CodebaseContextGatherer."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from genesis.surplus.code_audit import (
    _MAX_CONTEXT_CHARS,
    CodeAuditExecutor,
    CodebaseContextGatherer,
    _is_slop,
)
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
async def test_run_cmd_kills_process_on_timeout():
    """On subprocess timeout, the child is killed and reaped (not left running)."""
    db = AsyncMock()
    gatherer = CodebaseContextGatherer(db, repo_root="/tmp/fake")

    proc = MagicMock()
    proc.communicate = AsyncMock(side_effect=TimeoutError)
    proc.kill = MagicMock()
    proc.wait = AsyncMock()

    with patch(
        "genesis.surplus.code_audit.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        result = await gatherer._run_cmd("git", "log")

    assert result == ""
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


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
    """Context is truncated to _MAX_CONTEXT_CHARS (6000)."""
    db = AsyncMock()
    cursor_mock = AsyncMock()
    cursor_mock.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=cursor_mock)

    gatherer = CodebaseContextGatherer(db, repo_root="/tmp/fake")

    with patch.object(gatherer, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
        mock_cmd.side_effect = [
            "x" * 3000,
            "y" * 3000,
            "z" * 3000,
        ]
        ctx = await gatherer.gather()

    assert len(ctx) <= _MAX_CONTEXT_CHARS + 100  # cap + truncation notice
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
    assert results[0]["category"] == "other"


# ---------------------------------------------------------------------------
# Category passthrough
# ---------------------------------------------------------------------------


def test_parse_findings_passes_through_valid_category():
    """A known taxonomy category survives parsing unchanged."""
    findings_json = json.dumps([
        {"file": "a.py", "severity": "high", "category": "async_state",
         "suggestion": "Swallowed error in poll loop", "confidence": 0.9},
    ])
    results = CodeAuditExecutor._parse_findings(
        findings_json, provider="test", drive_alignment="competence",
    )
    assert len(results) == 1
    assert results[0]["category"] == "async_state"


def test_parse_findings_missing_category_defaults_to_other():
    """A finding without a category gets 'other'."""
    findings_json = json.dumps([
        {"file": "a.py", "severity": "low",
         "suggestion": "Unused import asyncio", "confidence": 0.9},
    ])
    results = CodeAuditExecutor._parse_findings(
        findings_json, provider="test", drive_alignment="competence",
    )
    assert results[0]["category"] == "other"


def test_parse_findings_garbage_category_normalized_to_other():
    """An unknown or non-string category is normalized to 'other'."""
    findings_json = json.dumps([
        {"file": "a.py", "severity": "low", "category": "vibes",
         "suggestion": "Finding one", "confidence": 0.9},
        {"file": "b.py", "severity": "low", "category": 42,
         "suggestion": "Finding two", "confidence": 0.9},
        {"file": "c.py", "severity": "low", "category": ["tests"],
         "suggestion": "Finding three", "confidence": 0.9},
    ])
    results = CodeAuditExecutor._parse_findings(
        findings_json, provider="test", drive_alignment="competence",
    )
    assert [r["category"] for r in results] == ["other", "other", "other"]


def test_parse_findings_category_case_insensitive():
    """Category matching normalizes case/whitespace before validating."""
    findings_json = json.dumps([
        {"file": "a.py", "severity": "low", "category": " Structural ",
         "suggestion": "Duplicate helper pair", "confidence": 0.9},
    ])
    results = CodeAuditExecutor._parse_findings(
        findings_json, provider="test", drive_alignment="competence",
    )
    assert results[0]["category"] == "structural"


@pytest.mark.asyncio
async def test_prompt_schema_includes_critical_and_category():
    """The inline prompt schema allows 'critical' severity and asks for a category."""
    routing_result = MagicMock()
    routing_result.success = True
    routing_result.content = "[]"
    routing_result.provider_used = "test"
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
        await executor.execute(_make_task())

    call_site, messages = router.route_call.call_args.args
    assert call_site == "36_code_auditor"
    user_prompt = messages[-1]["content"]
    assert '"critical|high|medium|low"' in user_prompt
    assert '"category"' in user_prompt
    assert "structural|async_state|error_handling|tests|security|other" in user_prompt


# ---------------------------------------------------------------------------
# Audit Targets inventory (fan-in / god-modules)
# ---------------------------------------------------------------------------


async def _index_db(*, seed: bool) -> aiosqlite.Connection:
    """In-memory DB with the real code index tables (see codebase/indexer.py)."""
    from genesis.codebase.indexer import ensure_tables

    conn = await aiosqlite.connect(":memory:")
    await ensure_tables(conn)
    if seed:
        modules = [
            ("src/genesis/routing/router.py", "routing", "router"),
            ("src/genesis/memory/store.py", "memory", "store"),
            ("src/genesis/surplus/types.py", "surplus", "types"),
            ("src/genesis/runtime/bootstrap.py", "runtime", "bootstrap"),
        ]
        for path, pkg, name in modules:
            await conn.execute(
                "INSERT INTO code_modules "
                "(path, package, module_name, loc, file_mtime, last_indexed_at) "
                "VALUES (?, ?, ?, 100, 0.0, '2026-07-10T00:00:00')",
                (path, pkg, name),
            )
        # Fan-in: genesis.routing.router imported by 3 distinct sources,
        # genesis.memory.store by 1.
        imports = [
            ("src/genesis/memory/store.py", "genesis.routing.router"),
            ("src/genesis/surplus/types.py", "genesis.routing.router"),
            ("src/genesis/runtime/bootstrap.py", "genesis.routing.router"),
            ("src/genesis/runtime/bootstrap.py", "genesis.memory.store"),
            # God-module: bootstrap.py imports from 5 distinct top-level
            # genesis packages (routing, memory, surplus, ego, channels).
            ("src/genesis/runtime/bootstrap.py", "genesis.surplus.types"),
            ("src/genesis/runtime/bootstrap.py", "genesis.ego.core"),
            ("src/genesis/runtime/bootstrap.py", "genesis.channels.telegram"),
            # Relative import stored unresolved — must NOT count as internal.
            ("src/genesis/routing/router.py", "types"),
            # Stdlib import — must not appear anywhere.
            ("src/genesis/routing/router.py", "asyncio"),
        ]
        for source, target in imports:
            await conn.execute(
                "INSERT INTO code_imports (source_path, target_module) "
                "VALUES (?, ?)",
                (source, target),
            )
        await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_gatherer_audit_targets_from_seeded_index():
    """Seeded code_imports produce the Audit Targets section in context."""
    conn = await _index_db(seed=True)
    try:
        gatherer = CodebaseContextGatherer(conn, repo_root="/tmp/fake")
        with patch.object(gatherer, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.side_effect = ["log", "diff"]
            ctx = await gatherer.gather()
    finally:
        await conn.close()

    assert "## Audit Targets (fan-in / god-modules)" in ctx
    # Fan-in leader with its distinct-importer count
    assert "genesis.routing.router: 3" in ctx
    # God-module (5 distinct internal top-level packages)
    assert "src/genesis/runtime/bootstrap.py: 5 packages" in ctx
    assert "asyncio" not in ctx.split("## Audit Targets")[1]


@pytest.mark.asyncio
async def test_gatherer_audit_targets_god_module_threshold():
    """Sources importing from <5 internal packages are not god-modules."""
    conn = await _index_db(seed=True)
    try:
        gatherer = CodebaseContextGatherer(conn, repo_root="/tmp/fake")
        targets = await gatherer._get_audit_targets()
    finally:
        await conn.close()

    god_section = targets.split("God-modules")[1]
    # bootstrap.py qualifies (5 packages); store.py (1 import) must not.
    assert "src/genesis/runtime/bootstrap.py" in god_section
    assert "src/genesis/memory/store.py" not in god_section


@pytest.mark.asyncio
async def test_gatherer_empty_index_omits_audit_targets():
    """Empty index tables: no crash, no Audit Targets section."""
    conn = await _index_db(seed=False)
    try:
        gatherer = CodebaseContextGatherer(conn, repo_root="/tmp/fake")
        with patch.object(gatherer, "_run_cmd", new_callable=AsyncMock) as mock_cmd:
            mock_cmd.side_effect = ["log", "diff", "files"]
            ctx = await gatherer.gather()
    finally:
        await conn.close()

    assert "Audit Targets" not in ctx
    assert "Recent Commits" in ctx


@pytest.mark.asyncio
async def test_gatherer_missing_index_tables_omit_audit_targets():
    """A DB without the code index tables degrades to an empty inventory."""
    conn = await aiosqlite.connect(":memory:")
    try:
        gatherer = CodebaseContextGatherer(conn, repo_root="/tmp/fake")
        targets = await gatherer._get_audit_targets()
    finally:
        await conn.close()

    assert targets == ""
