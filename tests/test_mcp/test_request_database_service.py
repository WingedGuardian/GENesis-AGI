"""Real FastMCP clients over scratch SQLite; no provider or live-store calls."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from genesis.db.connection import get_db
from genesis.db.request_database import RequestDatabase
from genesis.db.schema import create_all_tables
from genesis.observability.mcp_middleware import InstrumentationMiddleware

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def service(tmp_path):
    path = tmp_path / "service.db"
    setup = await get_db(path)
    await create_all_tables(setup)
    await setup.execute("CREATE TABLE samples (value TEXT)")
    await setup.commit()
    await setup.close()
    db = RequestDatabase(path, max_units=4)
    server = FastMCP("scoped-test")
    server.add_middleware(InstrumentationMiddleware(None, "test", db=db))

    @server.tool()
    async def save(value: str, fail: bool = False) -> str:
        await db.execute("INSERT INTO samples VALUES (?)", (value,))
        if fail:
            raise ValueError("synthetic failure")
        return value

    @server.tool()
    async def read() -> list[str]:
        return [row[0] for row in await db.execute_fetchall("SELECT value FROM samples")]

    try:
        yield server, db
    finally:
        await db.close()


async def test_service_commits_success_and_rolls_back_failure_without_tracker(service):
    server, db = service
    async with Client(server) as client:
        assert (await client.call_tool("save", {"value": "kept"})).data == "kept"
        with pytest.raises(ToolError, match="synthetic failure"):
            await client.call_tool("save", {"value": "discarded", "fail": True})
        assert (await client.call_tool("read")).data == ["kept"]
    observer = await get_db(db.path)
    try:
        assert [row[0] for row in await observer.execute_fetchall("SELECT value FROM samples")] == ["kept"]
    finally:
        await observer.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_two_clients_do_not_share_pending_writes(service, cancel):
    server, db = service
    entered, release = asyncio.Event(), asyncio.Event()

    @server.tool()
    async def interrupted() -> str:
        await db.execute("INSERT INTO samples VALUES ('partial')")
        entered.set()
        await release.wait()
        raise ValueError("interrupted write")

    async with Client(server) as first, Client(server) as second:
        request_id = first.session._request_id
        task = asyncio.create_task(first.call_tool("interrupted"))
        await entered.wait()
        assert (await second.call_tool("read")).data == []
        if cancel:
            await first.cancel(request_id, reason="synthetic cancellation")
            task.cancel()
        else:
            release.set()
        with pytest.raises(asyncio.CancelledError if cancel else ToolError):
            await task
        # A subsequent write waits for rollback if the cancellation notification
        # was accepted before the server finished cleanup. Local task.cancel()
        # alone sends no MCP cancellation and is only a lost-reply simulation.
        await second.call_tool("save", {"value": "peer"})
        assert (await second.call_tool("read")).data == ["peer"]


async def test_finalization_error_cannot_report_success(service, monkeypatch):
    server, db = service
    tracker = Mock()
    server.middleware[0]._tracker = tracker
    original = db._open

    async def broken_commit(lease):
        await original(lease)
        lease.connection.commit = AsyncMock(side_effect=RuntimeError("commit rejected"))

    monkeypatch.setattr(db, "_open", broken_commit)
    async with Client(server) as client:
        with pytest.raises(ToolError, match="commit rejected"):
            await client.call_tool("save", {"value": "not-durable"})
    tracker.record.assert_called_once()
    assert tracker.record.call_args.kwargs["success"] is False
    monkeypatch.setattr(db, "_open", original)
    async with db.unit_of_work():
        assert await db.execute_fetchall("SELECT * FROM samples") == []


async def test_unknown_tool_and_error_shaped_result_obey_boundary(service):
    server, db = service

    @server.tool()
    async def error_shaped() -> dict:
        await db.execute("INSERT INTO samples VALUES ('normal-return')")
        return {"error": "application result, not an exception"}

    async with Client(server) as client:
        with pytest.raises(ToolError):
            await client.call_tool("no_such_tool")
        result = await client.call_tool("error_shaped")
        assert result.data["error"].startswith("application result")
        assert (await client.call_tool("read")).data == ["normal-return"]
    assert not db._owners and not db._workers


async def test_explicit_commit_survives_lost_reply_and_manager_restart(service):
    server, db = service
    committed = asyncio.Event()

    @server.tool()
    async def lose_reply() -> str:
        await db.execute("INSERT INTO samples VALUES ('durable-prefix')")
        await db.commit()
        await db.execute("INSERT INTO samples VALUES ('tail')")
        committed.set()
        await asyncio.Event().wait()
        return "unreachable"

    async with Client(server) as client:
        task = asyncio.create_task(client.call_tool("lose_reply"))
        await committed.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await db.close()
    restarted = RequestDatabase(db.path, max_units=1)
    try:
        async with restarted.unit_of_work():
            rows = await restarted.execute_fetchall("SELECT value FROM samples")
        assert [row[0] for row in rows] == ["durable-prefix"]
    finally:
        await restarted.close()


async def test_two_clients_observe_success_transaction_only_after_finalization(service):
    server, db = service
    entered, release = asyncio.Event(), asyncio.Event()

    @server.tool()
    async def pair() -> str:
        await db.execute("INSERT INTO samples VALUES ('first')")
        entered.set()
        await release.wait()
        await db.execute("INSERT INTO samples VALUES ('second')")
        return "done"

    async with Client(server) as first, Client(server) as second:
        task = asyncio.create_task(first.call_tool("pair"))
        await entered.wait()
        assert (await second.call_tool("read")).data == []
        release.set()
        assert (await task).data == "done"
        assert (await second.call_tool("read")).data == ["first", "second"]


async def test_scoped_boundary_keeps_freshness_guard_without_metrics(service, monkeypatch):
    from genesis.observability import mcp_spawn_identity, mcp_staleness_guard_config

    server, db = service
    monkeypatch.setattr(mcp_spawn_identity, "_spawn_commit", "01234567")
    monkeypatch.setattr(mcp_spawn_identity, "_spawn_at", "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(mcp_staleness_guard_config, "effective_mode", lambda: "block")
    read_deploy = AsyncMock(return_value=("2026-01-02T00:00:00+00:00", "fedcba98"))
    monkeypatch.setattr("genesis.db.crud.update_history.last_successful_update", read_deploy)
    entered = []

    @server.tool()
    async def procedure_store() -> str:
        entered.append(True)
        return "must not run"

    async with Client(server) as client:
        with pytest.raises(ToolError, match="stale"):
            await client.call_tool("procedure_store")
    assert not entered
    read_deploy.assert_awaited_once_with(db)
    assert not db._owners and not db._workers
