"""Bound client contracts on actual authenticated TCP, including cancellation."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import httpx2
import pytest
from mcp.client import streamable_http
from mcp.shared.exceptions import MCPError
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import request_response

from genesis.caller_context import CallerContext
from genesis.mcp.shared_client import _target, serve_stdio
from genesis.mcp.shared_client._connection import RemotePeer
from genesis.mcp.shared_paths import SharedPathError
from tests.test_mcp.test_database_target_http import TARGET_KEY, target
from tests.test_mcp.test_plan_bookmark_peer import META, settled
from tests.test_mcp.test_plan_bookmark_peer import bookmark_app as bookmark_app
from tests.test_mcp.test_plan_bookmark_peer import bookmark_tcp as bookmark_tcp
from tests.test_mcp.test_plan_bookmark_peer import database as database
from tests.test_mcp.test_plan_bookmark_peer import memory_service as memory_service


def bound_peer(runtime, owners):
    return RemotePeer(owners, SimpleNamespace(http=runtime.config, shutdown_grace_s=.5),
        lambda: dict(runtime.headers),
        expected_catalog_revision=runtime.app.state.shared_catalog.revision(CallerContext(dispatched=False)),
        pending_file=runtime.pending, memory_target=target(runtime)["target"])


async def invoke(remote, identifier=3, *, era="2026-07-28", extra=None):
    metadata = {**(META if era == "2026-07-28" else {}), "progressToken": 0, "custom": {"kept": True}, **(extra or {})}
    return await remote.request(SimpleNamespace(request_id=identifier), "tools/call", {
        "name": "procedure_recall", "arguments": {"task_description": "synthetic-task"}, "_meta": metadata})


@pytest.mark.parametrize("era", ["2026-07-28", "2025-06-18"])
async def test_bound_peer_stamps_exact_captured_target_and_preserves_metadata(bookmark_tcp, era):
    runtime = bookmark_tcp
    async with anyio.create_task_group() as owners:
        remote = bound_peer(runtime, owners)
        try:
            if era == "2025-06-18":
                await remote.request(SimpleNamespace(request_id=0), "initialize", {
                    "protocolVersion": era, "capabilities": {},
                    "clientInfo": {"name": "fixture", "version": "1"}}, initialize_version=era)
                await remote.notify(None, "notifications/initialized", None)
            assert not (await invoke(remote, 0, era=era)).get("isError")
            assert len(await settled(runtime)) == 1 and not runtime.pending.exists()
            frame = next(frame for frame in runtime.frames if frame["method"] == "tools/call")
            assert type(frame["id"]) is int and frame["id"] == 0
            assert frame["params"]["_meta"] == {
                **(META if era == "2026-07-28" else {}), "progressToken": 0, "custom": {"kept": True},
                TARGET_KEY: target(runtime), "genesis/planBookmarkPending": {"version": 1, "path": str(runtime.pending)}}
            assert remote.url == runtime.config.origin + "/mcp/v3" and runtime.status_reads == 1
        finally:
            await remote.close()
            owners.cancel_scope.cancel()
    assert all(path in {"/status", "/mcp/v3"} for _, path in runtime.exchanges)
    if era == "2025-06-18":
        assert ("GET", "/mcp/v3") in runtime.exchanges and ("DELETE", "/mcp/v3") in runtime.exchanges


@pytest.mark.parametrize("value", [None, {}, "match", "foreign"])
async def test_reserved_target_never_acquires_a_generation_or_reaches_http(bookmark_tcp, value):
    runtime = bookmark_tcp
    if value == "match":
        value = target(runtime)
    elif value == "foreign":
        value = {"version": 1, "target": "0" * 64}
    async with anyio.create_task_group() as owners:
        remote = bound_peer(runtime, owners)
        try:
            with pytest.raises(SharedPathError, match="reserved"):
                await invoke(remote, extra={TARGET_KEY: value})
            assert remote.current is None and runtime.exchanges == [] and runtime.frames == []
            assert runtime.pending.exists() and runtime.decoy.exists()
        finally:
            await remote.close()
            owners.cancel_scope.cancel()


@pytest.mark.parametrize("new_generation", [False, True])
@pytest.mark.parametrize("mode", ["missing", "different", "version", "malformed"])
async def test_bound_status_refuses_before_dispatch_on_each_generation(bookmark_tcp, new_generation, mode):
    runtime = bookmark_tcp
    route = next(route for route in runtime.app.routes if route.path == "/status")
    original = route.endpoint
    async with anyio.create_task_group() as owners:
        remote = bound_peer(runtime, owners)
        try:
            if new_generation:
                assert not (await invoke(remote, "before")).get("isError")
                assert len(await settled(runtime)) == 1
                await remote.close()
                runtime.pending.write_text(json.dumps({"title": "Must remain"}))

            async def status(request):
                response = await original(request)
                body = json.loads(response.body)
                if mode == "missing":
                    del body["database_target_contract"]
                elif mode == "different":
                    body["database_target_contract"]["target"] = "0" * 64
                elif mode == "version":
                    body["database_target_contract"]["version"] = True
                else:
                    body["database_target_contract"] = []
                return JSONResponse(body)

            route.endpoint, route.app = status, request_response(status)
            before = len(runtime.frames)
            with pytest.raises(MCPError, match="could not initialize"):
                await invoke(remote, "refused")
            assert len(runtime.frames) == before and runtime.status_reads == 1 + int(new_generation)
            assert len(await settled(runtime)) == int(new_generation) and runtime.pending.exists() and runtime.decoy.exists()
        finally:
            await remote.close()
            owners.cancel_scope.cancel()


async def test_actual_slow_stream_deadline_closes_client_and_cancels_response(bookmark_tcp, monkeypatch):
    runtime = bookmark_tcp
    route = next(route for route in runtime.app.routes if route.path == "/status")
    original = route.endpoint
    finished, progress, clients = anyio.Event(), [], []
    actual_client = _target.httpx2.AsyncClient

    class ObservedClient(actual_client):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            clients.append(self)

    async def status(request):
        response = await original(request)
        # Keep each read moving beyond the actual SDK operation budget.
        # An artificial startup window can expire before this stream exists.
        pieces = [b" "] * (int(_target.MCP_DEFAULT_TIMEOUT / .04) + 5) + [response.body]

        async def stream():
            try:
                for index, piece in enumerate(pieces):
                    progress.append(index)
                    yield piece
                    # Faster than the per-read timeout; only the whole-check
                    # budget stops this continuously progressing response.
                    await anyio.sleep(.04)
                progress.append("complete")
            finally:
                finished.set()

        return StreamingResponse(stream(), media_type="application/json")

    route.endpoint, route.app = status, request_response(status)
    monkeypatch.setattr(_target.httpx2, "AsyncClient", ObservedClient)
    with pytest.raises(TimeoutError):
        await _target.select_memory_target(SimpleNamespace(http=runtime.config), lambda: dict(runtime.headers), runtime.db.path)
    with anyio.fail_after(3):
        await finished.wait()
    assert progress and "complete" not in progress and len(clients) == 1 and clients[0].is_closed
    assert await settled(runtime) == [] and runtime.pending.exists() and runtime.decoy.exists()


async def test_actual_preflight_ignores_proxy_environment(bookmark_tcp, monkeypatch):
    runtime = bookmark_tcp
    for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]:
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    assert await _target.select_memory_target(SimpleNamespace(http=runtime.config),
        lambda: dict(runtime.headers), runtime.db.path) == target(runtime)["target"]
    assert runtime.status_reads == 1 and runtime.frames == [] and runtime.pending.exists()


@pytest.mark.parametrize("service", ["memory", "health"])
def test_bound_peer_cannot_silently_drop_target_without_pending(service):
    with pytest.raises(ValueError, match="memory peer with pending"):
        RemotePeer(None, SimpleNamespace(http=SimpleNamespace(service=service, url="http://127.0.0.1:1/mcp")),
                   lambda: {}, memory_target="a" * 64)


@pytest.mark.parametrize("local", [False, True])
async def test_public_entry_rejects_target_on_nonmemory_or_nonlocal_frontend(local):
    settings = SimpleNamespace(http=SimpleNamespace(service="health"))
    with pytest.raises(ValueError, match="local memory frontend"):
        await serve_stdio(settings, lambda: {}, local_server=object() if local else None,
                          stage="health-reads" if local else None, memory_target="a" * 64)


@pytest.mark.parametrize("era", ["2026-07-28", "2025-06-18"])
async def test_bound_peer_reports_unknown_after_bookmark_commit_without_resending(bookmark_tcp, monkeypatch, era):
    runtime = bookmark_tcp
    original_stream = streamable_http.stream_within_origin
    dropped = []

    @asynccontextmanager
    async def lose_reply(client, method, url, **kwargs):
        async with original_stream(client, method, url, **kwargs) as response:
            frame = kwargs.get("json")
            if (method != "POST" or not isinstance(frame, dict) or frame.get("id") != "unknown"
                    or frame.get("method") != "tools/call" or dropped):
                yield response
                return
            response.raise_for_status()
            reply = await response.aread()
            assert b'"id":"unknown"' in reply.replace(b" ", b"")
            assert len(await settled(runtime)) == 1 and not runtime.pending.exists()
            dropped.append(response.status_code)
            yield httpx2.Response(502)

    monkeypatch.setattr(streamable_http, "stream_within_origin", lose_reply)
    async with anyio.create_task_group() as owners:
        remote = bound_peer(runtime, owners)
        try:
            if era == "2025-06-18":
                await remote.request(SimpleNamespace(request_id=0), "initialize", {
                    "protocolVersion": era, "capabilities": {},
                    "clientInfo": {"name": "fixture", "version": "1"}}, initialize_version=era)
                await remote.notify(None, "notifications/initialized", None)
            with anyio.fail_after(15), pytest.raises(MCPError, match="Outcome unknown"):
                await invoke(remote, "unknown", era=era)
            assert dropped == [200]
            assert not (await invoke(remote, "explicit-next", era=era)).get("isError")
            assert [frame["id"] for frame in runtime.frames if frame["method"] == "tools/call"] == [
                "unknown", "explicit-next",
            ]
            assert len(await settled(runtime)) == 1 and runtime.decoy.exists()
        finally:
            await remote.close()
            owners.cancel_scope.cancel()
