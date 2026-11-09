"""Authenticated SDK dispatch into the real memory guard and bookmark index."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import asyncio
import json
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import genesis.caller_context as identity
from genesis.bookmark.manager import BookmarkManager
from genesis.caller_credentials import CallerResolver
from genesis.mcp.shared_http import SharedHTTPConfig, build_http_app
from tests.test_mcp.test_request_service_tool_bodies import database as database
from tests.test_mcp.test_request_service_tool_bodies import memory_service as memory_service
from tests.test_mcp.test_shared_http_runtime import runtime as runtime
from tests.test_mcp.test_shared_http_runtime import unpack

KEY = "genesis/planBookmarkPending"
META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {}}


@asynccontextmanager
async def lifespan(app):
    ready = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def run():
        try:
            async with app.router.lifespan_context(app):
                ready.set_result(None)
                await stop.wait()
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
            raise

    owner = asyncio.create_task(run())
    try:
        await ready
        yield
    finally:
        stop.set()
        await owner


@pytest.fixture
async def bookmark_app(database, memory_service, monkeypatch, tmp_path):
    server, memory, tracker = memory_service
    for name in identity.IDENTITY_ENV_VARS:
        monkeypatch.setenv(name, "fixture")
    monkeypatch.setattr(identity, "_shared_service", False)
    pending = tmp_path / "caller/.genesis/plan_bookmark_pending.json"
    pending.parent.mkdir(parents=True)
    pending.write_text(json.dumps({"title": "Caller plan", "session_id_hint": "11111111"}))
    (pending.parent / "sessions/11111111-1111-1111-1111-111111111111").mkdir(parents=True)
    decoy = tmp_path / "server/.genesis/plan_bookmark_pending.json"
    decoy.parent.mkdir(parents=True)
    decoy.write_text(json.dumps({"title": "Server decoy"}))
    monkeypatch.setattr(memory, "_PLAN_BOOKMARK_PENDING", decoy)
    manager = BookmarkManager(memory_store=SimpleNamespace(store=AsyncMock(return_value="memory")),
                              hybrid_retriever=None, db=database)
    monkeypatch.setattr(manager, "_trigger_extraction", AsyncMock())
    monkeypatch.setattr(memory, "_bookmark_mgr", manager)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    config = SharedHTTPConfig("memory", origin + "/mcp", 4, 2, 8, 30, 65536)
    app = await build_http_app(server,
        config=config,
        resolver=CallerResolver("f" * 43, {"external": "e" * 43, "validator": "v" * 43}),
        database=database, tracker=tracker, loaded_commit="a" * 40,
        started_at="2026-01-01T00:00:00+00:00", enabled=lambda: True)
    headers = {"Authorization": "Bearer " + "f" * 43,
               "X-Genesis-Caller": json.dumps({"dispatched": False, "supervised": False}),
               "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2026-07-28"}
    try:
        yield SimpleNamespace(app=app, config=config, headers=headers, listener=listener,
                              db=database, pending=pending, decoy=decoy, memory=memory)
    finally:
        listener.close()


@pytest.fixture
async def bookmark_http(bookmark_app):
    runtime = bookmark_app
    async with lifespan(runtime.app), httpx.AsyncClient(transport=httpx.ASGITransport(runtime.app),
        base_url=runtime.config.origin, headers=runtime.headers) as client:
        runtime.client = client
        yield runtime


async def call(runtime, *, metadata=None, arguments=None, endpoint="/mcp/v1", headers=None):
    return await runtime.client.post(endpoint,
        headers={"mcp-method": "tools/call", "mcp-name": "procedure_recall", **(headers or {})},
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "procedure_recall", "arguments": arguments if arguments is not None else {"task_description": "synthetic-task"},
            "_meta": {**META, **(metadata or {})}}})


async def settled(runtime):
    while runtime.db._jobs:
        await asyncio.gather(*tuple(runtime.db._jobs))
    async with runtime.db.unit_of_work():
        rows = await runtime.db.execute_fetchall("SELECT topic,source,cc_session_id FROM session_bookmarks")
    return [tuple(row) for row in rows]


async def test_authenticated_request_consumes_caller_marker_after_valid_body(bookmark_http):
    runtime = bookmark_http
    reply = unpack(await call(runtime, metadata={KEY: {"version": 1, "path": str(runtime.pending)}}))
    assert reply["id"] == 3 and not reply["result"].get("isError")
    assert await settled(runtime) == [("Caller plan", "plan", "11111111-1111-1111-1111-111111111111")]
    assert not runtime.pending.exists() and runtime.decoy.exists()


@pytest.mark.parametrize("caller", [
    {"dispatched": True, "supervised": False, "session_id": "background", "origin": "external_untrusted"},
    {"dispatched": True, "supervised": True, "session_id": "attended"},
])
async def test_authenticated_dispatched_requests_preserve_bookmark_effect(bookmark_http, caller):
    runtime = bookmark_http
    reply = unpack(await call(runtime, metadata={KEY: {"version": 1, "path": str(runtime.pending)}},
                              headers={"X-Genesis-Caller": json.dumps(caller)}))
    assert not reply["result"].get("isError")
    assert len(await settled(runtime)) == 1
    assert not runtime.pending.exists() and runtime.decoy.exists()


@pytest.mark.parametrize("reason", ["absent", "invalid-arguments", "old-endpoint", "external", "validator", "invalid-auth", "labelled-forwarder"])
async def test_ineligible_or_invalid_calls_do_not_consume_either_marker(bookmark_http, reason):
    runtime = bookmark_http
    options = {"metadata": {KEY: {"version": 1, "path": str(runtime.pending)}}}
    if reason == "absent":
        options["metadata"] = {}
    elif reason == "invalid-arguments":
        options["arguments"] = {}
    elif reason == "old-endpoint":
        options["endpoint"] = "/mcp"
    elif reason in {"external", "validator"}:
        runtime.client.headers.pop("X-Genesis-Caller")
        options["headers"] = {"Authorization": "Bearer " + reason[0] * 43}
    elif reason == "invalid-auth":
        options["headers"] = {"Authorization": "Bearer invalid"}
    else:
        options["headers"] = {"X-Genesis-Caller": json.dumps({"dispatched": False, "supervised": False, "profile": "codex"})}
    if reason in {"external", "validator"}:
        baseline = await call(runtime, headers=options["headers"])
        assert baseline.status_code == 200 and not unpack(baseline)["result"].get("isError")
        assert await settled(runtime) == []
        assert runtime.pending.exists() and runtime.decoy.exists()
    response = await call(runtime, **options)
    if reason in {"external", "validator"}:
        assert response.status_code == 200
        assert "Pending bookmarks require an authenticated memory forwarder" in str(unpack(response))
    if reason == "absent":
        assert not unpack(response)["result"].get("isError")
    elif response.status_code == 200:
        reply = unpack(response)
        assert reply.get("error") or reply["result"].get("isError")
    else:
        assert response.status_code in {400, 401}
    assert await settled(runtime) == []
    assert runtime.pending.exists() and runtime.decoy.exists()


@pytest.mark.parametrize("value", [None, {}, {"version": True, "path": "/x/.genesis/plan_bookmark_pending.json"},
                                   {"version": 1, "path": "relative/.genesis/plan_bookmark_pending.json"}])
async def test_malformed_metadata_refuses_before_body(bookmark_http, value):
    reply = unpack(await call(bookmark_http, metadata={KEY: value}))
    assert reply.get("error") or reply["result"].get("isError")
    assert await settled(bookmark_http) == []
    assert bookmark_http.pending.exists() and bookmark_http.decoy.exists()


async def test_memory_status_advertises_exact_feature_without_paths(bookmark_http):
    response = await bookmark_http.client.get("/status")
    assert response.status_code == 200
    value = response.json()["plan_bookmark_pending_version"]
    assert type(value) is int and value == 1
    assert str(bookmark_http.pending) not in response.text
    assert await settled(bookmark_http) == []


async def test_invalid_then_valid_then_no_metadata_does_not_reuse_path(bookmark_http):
    runtime = bookmark_http
    metadata = {KEY: {"version": 1, "path": str(runtime.pending)}}
    reply = unpack(await call(runtime, metadata=metadata, arguments={}))
    assert reply["result"].get("isError") and await settled(runtime) == []
    assert not unpack(await call(runtime, metadata=metadata))["result"].get("isError")
    assert len(await settled(runtime)) == 1
    runtime.pending.write_text(json.dumps({"title": "Must remain"}))
    assert not unpack(await call(runtime))["result"].get("isError")
    assert len(await settled(runtime)) == 1 and runtime.pending.exists() and runtime.decoy.exists()


async def test_health_status_does_not_advertise_memory_feature(runtime):
    response = await runtime.client.get("/status")
    assert response.status_code == 200 and "plan_bookmark_pending_version" not in response.json()


async def test_overlapping_requests_keep_distinct_inputs(bookmark_http, tmp_path):
    runtime = bookmark_http
    other = tmp_path / "other/.genesis/plan_bookmark_pending.json"
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps({"title": "Other caller", "session_id_hint": "22222222"}))
    (other.parent / "sessions/22222222-2222-2222-2222-222222222222").mkdir(parents=True)
    replies = await asyncio.gather(*(
        call(runtime, metadata={KEY: {"version": 1, "path": str(path)}})
        for path in (runtime.pending, other)
    ))
    assert all(not unpack(reply)["result"].get("isError") for reply in replies), [unpack(reply) for reply in replies]
    assert sorted(await settled(runtime)) == [
        ("Caller plan", "plan", "11111111-1111-1111-1111-111111111111"),
        ("Other caller", "plan", "22222222-2222-2222-2222-222222222222"),
    ]
    assert not runtime.pending.exists() and not other.exists() and runtime.decoy.exists()
    assert not unpack(await call(runtime))["result"].get("isError")
    assert len(await settled(runtime)) == 2 and runtime.decoy.exists()
