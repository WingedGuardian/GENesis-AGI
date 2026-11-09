"""Tests for the skill_usage_status MCP wrapper (issue #3197).

The scan itself is covered in tests/test_learning/test_skill_usage.py; these
pin the wrapper: single-flight, progress reporting, the cc_sessions join and
registration.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.learning.skills import usage
from genesis.mcp.health import skill_usage_status as tool


def _transcript(projects, sid: str, skill: str) -> None:
    path = projects / "p" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat()
    path.write_text(
        json.dumps(
            {
                "sessionId": sid,
                "entrypoint": "cli",
                "type": "assistant",
                "timestamp": stamp,
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "t", "name": "Skill", "input": {"skill": skill}}
                    ]
                },
            }
        )
        + "\n"
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    (claude / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(claude))
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps({"tier1": [{"name": "alpha", "tier": 1, "path": ".claude/skills/alpha"}]})
    )
    population = usage.load_population(
        catalog_path=catalog,
        user_skills_dir=tmp_path / "none",
        plugins_manifest=tmp_path / "none.json",
    )
    monkeypatch.setattr(usage, "_default_population", lambda: population)
    return claude / "projects"


class FakeCtx:
    def __init__(self):
        self.calls = []

    async def report_progress(self, progress, total=None, message=None):
        self.calls.append((progress, total, message))


@pytest.mark.asyncio
async def test_scans_without_db_and_reports_progress(home, monkeypatch):
    for i in range(3):
        _transcript(home, f"s{i}", "alpha")
    monkeypatch.setattr(tool, "_PROGRESS_EVERY_S", 0.0)
    ctx = FakeCtx()
    svc = MagicMock()
    svc._db = None
    with patch("genesis.mcp.health_mcp._service", svc):
        rep = await tool._impl_skill_usage_status(14, ctx)
    assert rep["status"] == "partial"  # no DB: the injected columns are missing
    assert rep["db_join"] == "unavailable"
    assert {"input": "db", "path": "no database connection", "mode": "unavailable"} in rep[
        "input_issues"
    ]
    alpha = next(r for r in rep["skills"] if r["name"] == "alpha")
    assert alpha["loads"]["interactive"] == 3
    assert ctx.calls and ctx.calls[-1][:2] == (3, 3)


@pytest.mark.asyncio
async def test_joins_cc_sessions_when_db_is_wired(home):
    conn = await aiosqlite.connect(":memory:")
    await create_all_tables(conn)
    now = datetime.now(UTC)
    await conn.execute(
        "INSERT INTO cc_sessions (id, session_type, model, started_at, last_activity_at, metadata, status) "
        "VALUES (?, 'background_task', 'm', ?, ?, ?, 'completed')",
        ("r1", now.isoformat(), now.isoformat(), json.dumps({"skill_tags": ["alpha"]})),
    )
    await conn.execute(
        "INSERT INTO cc_sessions (id, session_type, model, started_at, last_activity_at, metadata, status) "
        "VALUES (?, 'background_task', 'm', ?, ?, ?, 'completed')",
        (
            "r2",
            (now - timedelta(days=40)).isoformat(),
            now.isoformat(),
            json.dumps({"skill_tags": ["alpha"]}),
        ),
    )
    await conn.commit()
    svc = MagicMock()
    svc._db = conn
    try:
        with patch("genesis.mcp.health_mcp._service", svc):
            rep = await tool._impl_skill_usage_status(14)
    finally:
        await conn.close()
    assert rep["db_join"] == "ok"
    assert rep["injected_sessions"] == 1
    alpha = next(r for r in rep["skills"] if r["name"] == "alpha")
    # injection is a column, never a use (owner ruling): no transcript use -> dead
    assert alpha["injected"] == 1 and alpha["state"] == "dead"


@pytest.mark.asyncio
async def test_second_call_while_scanning_returns_scan_in_progress(home):
    async with tool._scan_lock:
        rep = await tool._impl_skill_usage_status(14)
    assert rep["status"] == "scan_in_progress"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), 1e7])
async def test_rejects_invalid_window(home, bad):
    assert (await tool._impl_skill_usage_status(bad))["status"] == "error"


@pytest.mark.asyncio
async def test_tool_is_registered():
    from genesis.mcp.health_mcp import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "skill_usage_status" in tools


@pytest.mark.asyncio
async def test_cancelled_call_holds_the_lock_until_its_worker_thread_ends(home, monkeypatch):
    import asyncio
    import threading

    _transcript(home, "s0", "alpha")
    entered, release = threading.Event(), threading.Event()

    def blocking_scan(self, path, is_subagent=False):
        entered.set()
        release.wait(30)

    monkeypatch.setattr(usage.Scanner, "scan_file", blocking_scan)
    svc = MagicMock()
    svc._db = None
    with patch("genesis.mcp.health_mcp._service", svc):
        task = asyncio.create_task(tool._impl_skill_usage_status(14))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        assert tool._scan_lock.locked()  # the worker is still reading
        assert (await tool._impl_skill_usage_status(14))["status"] == "scan_in_progress"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not tool._scan_lock.locked()


@pytest.mark.asyncio
async def test_a_worker_failing_after_cancel_still_raises_the_cancellation(home, monkeypatch):
    import asyncio
    import threading

    _transcript(home, "s0", "alpha")
    entered, release = threading.Event(), threading.Event()

    def failing_scan(self, path, is_subagent=False):
        entered.set()
        release.wait(30)
        raise RuntimeError("worker failed")

    monkeypatch.setattr(usage.Scanner, "scan_file", failing_scan)
    svc = MagicMock()
    svc._db = None
    with patch("genesis.mcp.health_mcp._service", svc):
        task = asyncio.create_task(tool._impl_skill_usage_status(14))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()  # a second cancel during the wait
        await asyncio.sleep(0.05)
        assert tool._scan_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not tool._scan_lock.locked()
