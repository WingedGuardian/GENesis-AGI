"""Actual standalone bootstrap and instrumentation over owned migrated stores."""

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from genesis.db.crud import follow_ups
from genesis.observability.mcp_middleware import InstrumentationMiddleware
from tests.conftest import private_module
from tests.test_mcp.test_validator_private_tools import CREATE, stored
from tests.test_mcp.test_validator_verification_writer import recording as recording
from tests.test_scripts.test_pr_verification_closer import _doc

ROOT = Path(__file__).resolve().parents[2]


class BorrowedConnection:
    """The real MCP lifespan borrows a handle owned/closed by pytest's fixture.

    SQL/commit/rollback still hit the same real connection. Only closing belongs
    to the outer fixture, outside the in-process client's cancelled lifespan.
    This fixture does not qualify the standalone process's shutdown close.
    """

    def __init__(self, shared):
        self.shared = shared
        self.closes = 0

    def __getattr__(self, name):
        return getattr(self.shared, name)

    async def close(self):
        self.closes += 1


async def compose(recording, monkeypatch, tmp_path, *, profile="validator", has_db=True):
    from genesis import db
    from genesis.mcp import health

    path, shared, _ = recording
    borrowed = BorrowedConnection(shared)
    wrapper = private_module("owned_validator_health_bootstrap", ROOT / "scripts/genesis_mcp_server.py")
    source = {tool.name: tool for tool in await health.mcp.list_tools()}
    server = FastMCP("owned-bootstrapped-validator")
    for name in ("pr_verification_close", "follow_up_create", "provider_activity"):
        server.add_tool(source[name])
    monkeypatch.setattr(health, "mcp", server)
    for name in ("_service", "_event_bus", "_activity_tracker", "_job_retry_registry"):
        monkeypatch.setattr(health, name, None)
    monkeypatch.setattr(wrapper, "_DEFAULT_DB", path if has_db else tmp_path / "absent.sqlite")
    monkeypatch.setattr(wrapper, "_DEFAULT_STATUS", tmp_path / "absent-status.json")
    monkeypatch.setattr(wrapper, "clear_mcp_crash", lambda *_: None)
    monkeypatch.setattr(wrapper, "_init_standalone_health_router", lambda: None)

    async def no_actions(*_):
        pass

    async def open_owned(target, **kwargs):
        assert target == path and kwargs == {"foreign_keys": False}
        return borrowed

    monkeypatch.setattr(wrapper, "_init_standalone_health_actions", no_actions)
    monkeypatch.setattr(db.connection, "get_db", open_owned)
    monkeypatch.setattr("genesis.mcp.health.browser.async_cleanup", no_actions)
    captured = []
    monkeypatch.setattr(wrapper, "_run_mcp", lambda mcp, _: captured.append(mcp))
    wrapper._bootstrap_health({}, external_profile=profile)
    assert captured == [server]
    return server, health, borrowed


async def flush_if_scheduled(tracker):
    if tracker._flush_task is not None:
        await tracker._flush_task


async def test_preview_full_wrapper_does_not_commit_foreign_pending_work(recording, monkeypatch, tmp_path):
    path, shared, _ = recording
    fid = await follow_ups.create(
        shared, source="foreground_session", content="Synthetic foreign transaction", strategy="user_input_needed",
    )
    await shared.execute("UPDATE follow_ups SET priority='high' WHERE id=?", (fid,))
    server, health, borrowed = await compose(recording, monkeypatch, tmp_path)
    async with Client(server) as client:
        try:
            result = await client.call_tool("pr_verification_close", {
                "pr": 7, "evidence_json": json.dumps(_doc()),
            })
            assert result.data["dry_run"] is True
            tracker = health._activity_tracker
            await flush_if_scheduled(tracker)
            assert stored(path, fid)["priority"] == "medium", "foreign work was committed by a wrapper"
            assert shared.in_transaction
            assert tracker.persist_activity is False and tracker._db is borrowed
            assert tracker._write_batch == [] and tracker._flush_task is None
            assert tracker.summary("mcp.health.pr_verification_close")["calls"] == 1
        finally:
            await shared.rollback()
    assert borrowed.closes == 1


async def test_failed_owned_write_full_wrapper_does_not_rollback_foreign_pending_work(recording, monkeypatch, tmp_path):
    path, shared, _ = recording
    fid = await follow_ups.create(
        shared, source="foreground_session", content="Synthetic foreign transaction", strategy="user_input_needed",
    )
    await shared.execute("UPDATE follow_ups SET priority='high' WHERE id=?", (fid,))
    server, health, borrowed = await compose(recording, monkeypatch, tmp_path)
    async with Client(server) as client:
        try:
            with pytest.raises(ToolError):
                await client.call_tool("follow_up_create", CREATE)
            await flush_if_scheduled(health._activity_tracker)
            assert (await follow_ups.get_by_id(shared, fid))["priority"] == "high"
            assert shared.in_transaction
            assert stored(path, fid)["priority"] == "medium"
        finally:
            await shared.rollback()
    assert borrowed.closes == 1


async def test_cancelled_wrapper_preserves_foreign_pending_work(recording, monkeypatch, tmp_path):
    path, shared, _ = recording
    fid = await follow_ups.create(
        shared, source="foreground_session", content="Synthetic foreign transaction", strategy="user_input_needed",
    )
    await shared.execute("UPDATE follow_ups SET priority='high' WHERE id=?", (fid,))
    server, health, borrowed = await compose(recording, monkeypatch, tmp_path)
    async with Client(server):
        entered = asyncio.Event()

        async def blocking_body(context):
            entered.set()
            await asyncio.Event().wait()

        middleware = next(m for m in server.middleware if isinstance(m, InstrumentationMiddleware))
        context = SimpleNamespace(message=SimpleNamespace(name="pr_verification_close"))
        task = asyncio.create_task(middleware.on_call_tool(context, blocking_body))
        try:
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await flush_if_scheduled(health._activity_tracker)
            assert (await follow_ups.get_by_id(shared, fid))["priority"] == "high"
            assert shared.in_transaction and stored(path, fid)["priority"] == "medium"
        finally:
            if not task.done():
                task.cancel()
            await shared.rollback()
    assert borrowed.closes == 1


@pytest.mark.parametrize("profile", [None, "external"])
async def test_ordinary_health_keeps_transaction_and_activity_defaults(recording, monkeypatch, tmp_path, profile):
    path, shared, _ = recording
    fid = await follow_ups.create(
        shared, source="foreground_session", content="Synthetic ordinary control", strategy="user_input_needed",
    )
    await shared.execute("UPDATE follow_ups SET priority='high' WHERE id=?", (fid,))
    server, health, borrowed = await compose(recording, monkeypatch, tmp_path, profile=profile)
    async with Client(server) as client:
        await client.call_tool("provider_activity")
        tracker = health._activity_tracker
        await flush_if_scheduled(tracker)
        assert stored(path, fid)["priority"] == "high" and not shared.in_transaction
        with sqlite3.connect(path) as reader:
            assert reader.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0] == 1
        assert tracker.summary("mcp.health.provider_activity")["calls"] == 1
    assert borrowed.closes == 1


async def test_validator_keeps_history_reads_without_persisting_new_metrics(recording, monkeypatch, tmp_path):
    from datetime import UTC, datetime

    path, shared, _ = recording
    await shared.execute(
        "INSERT INTO activity_log (provider, latency_ms, success, cache_hit, created_at) VALUES (?, ?, ?, ?, ?)",
        ("synthetic.history", 12, 1, 0, datetime.now(UTC).isoformat()),
    )
    await shared.commit()
    server, health, borrowed = await compose(recording, monkeypatch, tmp_path)
    async with Client(server) as client:
        result = await client.call_tool("provider_activity")
        rows = result.structured_content["result"]
        assert any(row["provider"] == "synthetic.history" and row["calls"] == 1 for row in rows)
        tracker = health._activity_tracker
        tracker.record("synthetic.local", latency_ms=1, success=True)
        await flush_if_scheduled(tracker)
        with sqlite3.connect(path) as reader:
            assert reader.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0] == 1
        assert tracker.summary("synthetic.local")["calls"] == 1
        assert tracker._db is borrowed and tracker._flush_task is None
    assert borrowed.closes == 1


@pytest.mark.parametrize("profile", ["validator", "external"])
async def test_no_store_bootstrap_has_no_persistence_or_transaction_handle(recording, monkeypatch, tmp_path, profile):
    server, health, borrowed = await compose(recording, monkeypatch, tmp_path, profile=profile, has_db=False)
    async with Client(server) as client:
        assert (await client.call_tool("provider_activity")).data == []
        tracker = health._activity_tracker
        assert tracker._db is None and tracker._flush_task is None and tracker._write_batch == []
        middleware = next(m for m in server.middleware if isinstance(m, InstrumentationMiddleware))
        assert middleware._db is None
    assert borrowed.closes == 0
