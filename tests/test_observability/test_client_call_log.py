"""Owner-mode clients leave a durable record of every tool call, and nothing else changes.

The log middleware is on every server and decides per call, from
``caller_context()``, whether the caller is a labelled client. It records
client, server, tool, start time, how the call ended, and the id an allowlisted
writer tool returned; never the arguments. Recording never changes the tool's
outcome, survives cancellation, and never creates a database. Credential
lookups are stamped with the same label server-side.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import aiosqlite
import anyio
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

from genesis.observability.client_call_log import ClientCallLogMiddleware, result_ref
from genesis.observability.mcp_middleware import install_instrumentation

PROFILE_ENV = "GENESIS_CALLER_PROFILE"
# install_instrumentation adds the call log with the instrumentation, which every
# real bootstrap installs because it always passes an activity tracker.
UUID = "3f2a9c1e-0b7d-4e55-9a10-2b6c8d4e1f00"


@pytest.fixture
async def log_db(tmp_path, monkeypatch):
    path = tmp_path / "calls.db"
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(str(path)) as conn:
        await create_all_tables(conn)
        await conn.commit()
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: path)
    return path


def _rows(path):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT client, server, tool, outcome, result_ref FROM client_tool_calls ORDER BY id"
        ).fetchall()


def _server(arguments_seen: list) -> FastMCP:
    server = FastMCP("client-log-fixture")

    @server.tool()
    async def memory_store(content: str) -> str:  # fastmcp wraps a str as {"result": ...}
        arguments_seen.append(content)
        return UUID

    @server.tool()
    async def browser_run_js(script: str) -> dict:
        return {"result": "45b73349927b41a093c8ccdc46679f91", "url": "https://example.invalid"}

    @server.tool()
    async def boom(secret: str) -> str:
        raise ValueError("synthetic failure")

    @server.tool()
    async def refuse() -> dict:
        return {"error": "Refused: synthetic"}

    return server


async def test_a_supersession_report_records_the_stored_memory_id(log_db, monkeypatch):
    """memory_store returns a report when it supersedes; the row it wrote is its
    memory_id, not the report and not nothing."""
    monkeypatch.setenv(PROFILE_ENV, "codex")
    server = FastMCP("client-log-supersede-fixture")

    @server.tool()
    async def memory_store(content: str, supersedes: str | None = None) -> str | dict:
        if supersedes:
            return {"memory_id": UUID, "superseded": True, "old_id": supersedes, "new_id": UUID}
        return UUID

    install_instrumentation(server, Mock(), "memory", None)
    async with Client(server) as client:
        await client.call_tool("memory_store", {"content": "x", "supersedes": "0" * 32})
        await client.call_tool("memory_store", {"content": "y"})
    assert _rows(log_db) == [
        ("codex", "memory", "memory_store", "returned", UUID),
        ("codex", "memory", "memory_store", "returned", UUID),
    ]


async def test_labelled_calls_are_recorded_without_arguments(log_db, monkeypatch):
    monkeypatch.setenv(PROFILE_ENV, "codex")
    seen: list = []
    server = _server(seen)
    install_instrumentation(server, Mock(), "memory", None)
    async with Client(server) as client:
        await client.call_tool("memory_store", {"content": "hunter2-not-a-real-secret"})
        await client.call_tool("browser_run_js", {"script": "document.cookie"})
        await client.call_tool("refuse")
        with pytest.raises(ToolError):
            await client.call_tool("boom", {"secret": "also-not-real"})
    assert seen == ["hunter2-not-a-real-secret"]
    assert _rows(log_db) == [
        ("codex", "memory", "memory_store", "returned", UUID),
        # Not an allowlisted writer: a hex value under "result" is never kept.
        ("codex", "memory", "browser_run_js", "returned", None),
        # A refusal inside a normal return still reads "returned": outcome is how
        # the call ended, not whether the tool succeeded.
        ("codex", "memory", "refuse", "returned", None),
        ("codex", "memory", "boom", "raised", None),
    ]
    with sqlite3.connect(log_db) as conn:
        dump = "\n".join(conn.iterdump())
    assert "hunter2" not in dump and "also-not-real" not in dump and "cookie" not in dump


async def test_unlabelled_calls_log_nothing(log_db, monkeypatch):
    monkeypatch.delenv(PROFILE_ENV, raising=False)
    server = _server([])
    install_instrumentation(server, Mock(), "memory", None)
    async with Client(server) as client:
        await client.call_tool("memory_store", {"content": "x"})
    assert _rows(log_db) == []


async def test_the_label_is_read_per_call(log_db, monkeypatch):
    monkeypatch.delenv(PROFILE_ENV, raising=False)
    server = _server([])
    install_instrumentation(server, Mock(), "memory", None)
    async with Client(server) as client:
        await client.call_tool("memory_store", {"content": "x"})
        monkeypatch.setenv(PROFILE_ENV, "codex")
        await client.call_tool("memory_store", {"content": "y"})
    assert [r[0] for r in _rows(log_db)] == ["codex"]


async def test_reinstall_replaces_rather_than_stacks():
    server = _server([])
    install_instrumentation(server, Mock(), "memory", None)
    install_instrumentation(server, Mock(), "memory", None)
    assert sum(isinstance(m, ClientCallLogMiddleware) for m in server.middleware) == 1


async def test_a_cancelled_task_is_still_recorded(log_db, monkeypatch):
    monkeypatch.setenv(PROFILE_ENV, "codex")
    middleware = ClientCallLogMiddleware("memory")
    started = asyncio.Event()

    async def call_next(_context):
        started.set()
        await asyncio.sleep(30)

    context = SimpleNamespace(message=SimpleNamespace(name="memory_store"))
    task = asyncio.create_task(middleware.on_call_tool(context, call_next))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _rows(log_db) == [("codex", "memory", "memory_store", "cancelled", None)]


async def test_a_cancelled_anyio_scope_is_still_recorded(log_db, monkeypatch):
    monkeypatch.setenv(PROFILE_ENV, "codex")
    middleware = ClientCallLogMiddleware("memory")

    async def call_next(_context):
        await anyio.sleep(30)

    context = SimpleNamespace(message=SimpleNamespace(name="memory_store"))
    with anyio.move_on_after(0.05):
        await middleware.on_call_tool(context, call_next)
    assert _rows(log_db) == [("codex", "memory", "memory_store", "cancelled", None)]


async def test_a_failed_log_write_never_changes_the_tool_result(monkeypatch, caplog):
    monkeypatch.setenv(PROFILE_ENV, "codex")
    server = _server([])
    install_instrumentation(server, Mock(), "memory", None)

    def _broken():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("genesis.env.genesis_db_path", _broken)
    async with Client(server) as client:
        result = await client.call_tool("memory_store", {"content": "x"})
    assert result.structured_content == {"result": UUID}
    assert "could not record memory.memory_store for codex" in caplog.text


async def test_no_database_file_is_never_created(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(PROFILE_ENV, "codex")
    path = tmp_path / "absent.db"
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: path)
    server = _server([])
    install_instrumentation(server, Mock(), "memory", None)
    with caplog.at_level(logging.WARNING):
        async with Client(server) as client:
            await client.call_tool("memory_store", {"content": "x"})
            await client.call_tool("memory_store", {"content": "y"})
    assert not path.exists()
    assert caplog.text.count("are not being recorded") == 1  # warned once, not per call


async def test_an_unmigrated_table_warns_once(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(PROFILE_ENV, "codex")
    path = tmp_path / "old.db"
    async with aiosqlite.connect(str(path)):
        pass
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: path)
    server = _server([])
    install_instrumentation(server, Mock(), "memory", None)
    with caplog.at_level(logging.WARNING):
        async with Client(server) as client:
            await client.call_tool("memory_store", {"content": "x"})
            await client.call_tool("memory_store", {"content": "y"})
    assert caplog.text.count("table not migrated yet") == 1


@pytest.mark.parametrize(
    ("server", "tool", "structured", "expected"),
    [
        ("memory", "memory_store", {"result": UUID}, UUID),
        ("memory", "reference_store", {"result": UUID}, UUID),
        ("memory", "observation_write", {"result": "duplicate_skipped"}, None),
        (
            "health",
            "follow_up_create",
            {"id": "45b73349927b41a093c8ccdc46679f91"},
            "45b73349927b41a093c8ccdc46679f91",
        ),
        ("health", "ego_decision", {"decision_id": "d62e1351924a4455"}, "d62e1351924a4455"),
        ("health", "follow_up_create", {"id": "1234-5678-9012-3456"}, None),
        ("memory", "memory_store", {"result": "2026-11-08"}, None),
        ("memory", "memory_store", {"result": "555-123-4567"}, None),
        ("memory", "browser_run_js", {"result": "45b73349927b41a093c8ccdc46679f91"}, None),
        ("memory", "memory_store", None, None),
    ],
)
def test_result_ref_keeps_only_allowlisted_writer_ids(server, tool, structured, expected):
    result = SimpleNamespace(structured_content=structured)
    assert result_ref(server, tool, result) == expected


@pytest.fixture
async def audit_db(tmp_path):
    from genesis.db.schema import create_all_tables

    conn = await aiosqlite.connect(str(tmp_path / "audit.db"))
    await create_all_tables(conn)
    await conn.commit()
    yield conn
    await conn.close()


async def _audit_rows(conn):
    cur = await conn.execute("SELECT unit_id, accessor_context, client FROM credential_access_log")
    return await cur.fetchall()


def _knowledge():
    return importlib.import_module("genesis.mcp.memory.knowledge")


async def test_credential_audit_is_stamped_with_the_client(audit_db, monkeypatch):
    knowledge = _knowledge()
    monkeypatch.setenv(PROFILE_ENV, "codex")
    with patch.object(knowledge, "_memory_mod", return_value=SimpleNamespace(_db=audit_db)):
        await knowledge._log_credential_access(["u1"], accessor_context="client=claude")
    # The caller's free text cannot set the label; the server's context does.
    assert await _audit_rows(audit_db) == [("u1", "client=claude", "codex")]


async def test_credential_audit_without_a_client_reads_null(audit_db, monkeypatch):
    knowledge = _knowledge()
    monkeypatch.delenv(PROFILE_ENV, raising=False)
    with patch.object(knowledge, "_memory_mod", return_value=SimpleNamespace(_db=audit_db)):
        await knowledge._log_credential_access(["u2"], accessor_context=None)
    assert await _audit_rows(audit_db) == [("u2", None, None)]


async def test_credential_audit_keeps_the_row_before_the_column_exists(tmp_path, monkeypatch):
    knowledge = _knowledge()
    monkeypatch.setenv(PROFILE_ENV, "codex")
    async with aiosqlite.connect(str(tmp_path / "old.db")) as conn:
        await conn.execute(
            "CREATE TABLE credential_access_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "unit_id TEXT NOT NULL, accessor_context TEXT, accessed_at TEXT NOT NULL, "
            "query_match_score REAL)"
        )
        with patch.object(knowledge, "_memory_mod", return_value=SimpleNamespace(_db=conn)):
            await knowledge._log_credential_access(["u3"], accessor_context="ctx")
        cur = await conn.execute("SELECT unit_id, accessor_context FROM credential_access_log")
        assert await cur.fetchall() == [("u3", "ctx")]


def test_caller_profile_is_read_from_the_environment(monkeypatch):
    from genesis.caller_context import caller_context_from_env

    monkeypatch.setenv(PROFILE_ENV, "codex")
    assert caller_context_from_env().profile == "codex"
    monkeypatch.setenv(PROFILE_ENV, "")
    assert caller_context_from_env().profile is None
    monkeypatch.delenv(PROFILE_ENV)
    assert caller_context_from_env().profile is None


def test_dispatched_children_never_inherit_the_label():
    from genesis.cc.child_env import pin_dispatched_env

    env = pin_dispatched_env({PROFILE_ENV: "codex", "PATH": "/bin"})
    assert PROFILE_ENV not in env
    assert env["PATH"] == "/bin"


async def test_an_oversized_tool_name_is_stored_as_a_marker(log_db, monkeypatch):
    monkeypatch.setenv(PROFILE_ENV, "codex")
    middleware = ClientCallLogMiddleware("memory")

    async def call_next(_context):
        raise ValueError("unknown tool")

    context = SimpleNamespace(message=SimpleNamespace(name="x" * 5000))
    with pytest.raises(ValueError):
        await middleware.on_call_tool(context, call_next)
    assert _rows(log_db) == [("codex", "memory", "<tool name omitted: 5000 chars>", "raised", None)]


class _RefuseEverything(Middleware):
    """Stands in for ExternalProfileMiddleware, installed before instrumentation."""

    async def on_call_tool(self, context, call_next):
        raise ToolError("Tool is unavailable to this external client profile")


async def test_a_call_refused_by_an_earlier_middleware_is_still_recorded(log_db, monkeypatch):
    monkeypatch.setenv(PROFILE_ENV, "codex")
    server = _server([])
    server.add_middleware(_RefuseEverything())
    install_instrumentation(server, Mock(), "memory", None)
    assert isinstance(server.middleware[0], ClientCallLogMiddleware)  # outermost
    async with Client(server) as client:
        with pytest.raises(ToolError):
            await client.call_tool("memory_store", {"content": "x"})
    assert _rows(log_db) == [("codex", "memory", "memory_store", "raised", None)]


async def test_the_server_database_path_wins_over_the_default(tmp_path, log_db, monkeypatch):
    monkeypatch.setenv(PROFILE_ENV, "codex")
    server = _server([])
    install_instrumentation(
        server, SimpleNamespace(record=lambda *a, **k: None), "memory", SimpleNamespace(path=log_db)
    )
    other = tmp_path / "other.db"
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: other)
    async with Client(server) as client:
        await client.call_tool("memory_store", {"content": "x"})
    assert [r[2] for r in _rows(log_db)] == ["memory_store"]
    assert not other.exists()


def test_result_ref_strips_an_echoed_id():
    result = SimpleNamespace(structured_content={"decision_id": "  d62e1351924a4455\n"})
    assert result_ref("health", "ego_decision", result) == "d62e1351924a4455"


def test_module_children_never_inherit_the_label(monkeypatch):
    from genesis.modules.external.config import IPCConfig
    from genesis.modules.external.ipc import StdioIPCAdapter

    monkeypatch.setenv(PROFILE_ENV, "codex")
    for env in ({}, {"EXTRA": "1"}):
        ipc = StdioIPCAdapter(IPCConfig(command=["true"], env=env))
        spawn_env = ipc._spawn_env()
        assert PROFILE_ENV not in spawn_env
        assert spawn_env["PATH"]
    # Built at spawn time: a variable set after construction reaches the child.
    monkeypatch.setenv("GENESIS_PROBE_LATE", "1")
    assert ipc._spawn_env()["GENESIS_PROBE_LATE"] == "1"


async def test_a_cancel_during_the_write_still_lands_the_row(log_db, monkeypatch):
    # The ambiguous case: the tool returned, then the caller is cancelled while
    # the audit row is being written. Two cancels, so the second must not reach
    # the write either.
    monkeypatch.setenv(PROFILE_ENV, "codex")
    from genesis.db.crud import client_tool_calls

    writing = asyncio.Event()
    real_record = client_tool_calls.record

    async def slow_record(conn, **kwargs):
        writing.set()
        await asyncio.sleep(0.3)
        return await real_record(conn, **kwargs)

    monkeypatch.setattr(client_tool_calls, "record", slow_record)
    middleware = ClientCallLogMiddleware("memory")

    async def call_next(_context):
        return SimpleNamespace(structured_content={"result": UUID})

    context = SimpleNamespace(message=SimpleNamespace(name="memory_store"))
    task = asyncio.create_task(middleware.on_call_tool(context, call_next))
    await writing.wait()
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _rows(log_db) == [("codex", "memory", "memory_store", "returned", UUID)]


async def test_a_locked_database_costs_the_row_not_the_call(log_db, monkeypatch, caplog):
    monkeypatch.setenv(PROFILE_ENV, "codex")
    server = _server([])
    install_instrumentation(server, Mock(), "memory", None)
    holder = sqlite3.connect(log_db, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        loop = asyncio.get_running_loop()
        with caplog.at_level(logging.WARNING):
            async with Client(server) as client:
                began = loop.time()
                result = await client.call_tool("memory_store", {"content": "x"})
                elapsed = loop.time() - began
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert result.structured_content == {"result": UUID}
    assert elapsed < 2.0  # the one-second busy wait, not the general connection's
    assert "could not record memory.memory_store for codex" in caplog.text
    assert _rows(log_db) == []


class _RacingPath(type(Path())):
    """A database path whose file is removed between the existence check and the open."""

    def exists(self, *args, **kwargs):
        return True


async def test_a_database_removed_after_the_check_is_never_recreated(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(PROFILE_ENV, "codex")
    path = _RacingPath(tmp_path / "gone.db")
    middleware = ClientCallLogMiddleware("memory", db_path=path)

    async def call_next(_context):
        return None

    context = SimpleNamespace(message=SimpleNamespace(name="memory_store"))
    with caplog.at_level(logging.WARNING):
        await middleware.on_call_tool(context, call_next)
    assert not Path(path).exists()
    assert "could not record memory.memory_store for codex" in caplog.text
