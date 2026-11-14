"""Actual forwarder dispatcher and owned TCP service, including rollback."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import asyncio
import json
from types import SimpleNamespace

import anyio
import pytest
import uvicorn
from mcp.shared.exceptions import MCPError
from starlette.responses import JSONResponse

from genesis.caller_context import CallerContext
from genesis.mcp.shared_client._connection import RemotePeer
from genesis.mcp.shared_paths import SharedPathError
from tests.test_mcp.test_plan_bookmark_http import (
    KEY,
    META,
    settled,
)
from tests.test_mcp.test_plan_bookmark_http import (
    bookmark_app as bookmark_app,
)
from tests.test_mcp.test_plan_bookmark_http import (
    database as database,
)
from tests.test_mcp.test_plan_bookmark_http import (
    memory_service as memory_service,
)


@pytest.fixture
async def bookmark_tcp(bookmark_app):
    runtime = bookmark_app
    runtime.version, runtime.status_reads, runtime.frames, runtime.exchanges = 1, 0, [], []
    route = next(route for route in runtime.app.routes if route.path == "/status")
    original = route.endpoint

    async def status(request):
        response = await original(request)
        payload = json.loads(response.body)
        runtime.status_reads += 1
        if runtime.version == "missing":
            payload.pop("plan_bookmark_pending_version", None)
        else:
            payload["plan_bookmark_pending_version"] = runtime.version
        return JSONResponse(payload)

    # Route.app captured the endpoint at construction; replace that wrapper too.
    from starlette.routing import request_response

    route.endpoint, route.app = status, request_response(status)

    async def wire(scope, receive, send):
        pieces = []
        if scope["type"] == "http":
            runtime.exchanges.append((scope["method"], scope["path"]))

        async def observed():
            message = await receive()
            if scope.get("path") in {"/mcp/v1", "/mcp/v2"} and message["type"] == "http.request":
                pieces.append(message.get("body", b""))
                if not message.get("more_body", False):
                    runtime.frames.append(json.loads(b"".join(pieces)))
            return message

        await runtime.app(scope, observed, send)

    runner = uvicorn.Server(uvicorn.Config(wire, log_level="warning", access_log=False,
                                          lifespan="on", timeout_graceful_shutdown=.5))
    owner = asyncio.create_task(runner.serve(sockets=[runtime.listener]))
    try:
        async with asyncio.timeout(10):
            while not runner.started:
                assert not owner.done(), owner.exception() if owner.done() else None
                await asyncio.sleep(.01)
        yield runtime
    finally:
        runner.should_exit = True
        await asyncio.wait_for(owner, 10)


def peer(runtime, owners, *, pending=True):
    return RemotePeer(owners, SimpleNamespace(http=runtime.config, shutdown_grace_s=.5),
        lambda: dict(runtime.headers),
        expected_catalog_revision=runtime.app.state.shared_catalog.revision(CallerContext(dispatched=False)),
        pending_file=runtime.pending if pending else None)


async def request(remote, identifier=3, *, era="2026-07-28", meta=None):
    return await remote.request(SimpleNamespace(request_id=identifier), "tools/call", {
        "name": "procedure_recall", "arguments": {"task_description": "synthetic-task"},
        **({"_meta": {**META, **(meta or {})}} if era == "2026-07-28" else {}),
    })


@pytest.mark.parametrize("era", ["2026-07-28", "2025-06-18"])
async def test_owned_tcp_peer_consumes_exact_caller_file_and_preserves_wire(bookmark_tcp, era):
    runtime = bookmark_tcp
    async with anyio.create_task_group() as owners:
        remote = peer(runtime, owners)
        try:
            if era != "2026-07-28":
                await remote.request(SimpleNamespace(request_id=0), "initialize", {
                    "protocolVersion": era, "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}},
                    initialize_version=era)
                await remote.notify(None, "notifications/initialized", None)
            result = await request(remote, era=era, meta={"progressToken": "original", "custom": {"x": 7}})
            assert not result.get("isError")
            assert await settled(runtime) == [("Caller plan", "plan", "11111111-1111-1111-1111-111111111111")]
            assert not runtime.pending.exists() and runtime.decoy.exists()
            frames = [frame for frame in runtime.frames if frame["method"] == "tools/call"]
            assert len(frames) == 1 and type(frames[0]["id"]) is int and frames[0]["id"] == 3
            metadata = frames[0]["params"]["_meta"]
            assert metadata[KEY] == {"version": 1, "path": str(runtime.pending)}
            if era == "2026-07-28":
                assert metadata["progressToken"] == "original" and metadata["custom"] == {"x": 7}
            assert remote.url == runtime.config.origin + "/mcp/v2"
            await remote.close()
            rpc_paths = [(method, path) for method, path in runtime.exchanges if path != "/status"]
            assert rpc_paths and all(path == "/mcp/v2" for _, path in rpc_paths)
            if era == "2025-06-18":
                assert ("GET", "/mcp/v2") in rpc_paths and ("DELETE", "/mcp/v2") in rpc_paths
        finally:
            await remote.close()
            owners.cancel_scope.cancel()


@pytest.mark.parametrize("version", ["missing", True, 1.0, "1", 2, None])
@pytest.mark.parametrize("rollback", [False, True], ids=["initial", "new-generation"])
async def test_feature_refusal_precedes_tool_dispatch_each_generation(bookmark_tcp, version, rollback):
    runtime = bookmark_tcp
    async with anyio.create_task_group() as owners:
        remote = peer(runtime, owners)
        try:
            if rollback:
                assert not (await request(remote, "before" )).get("isError")
                assert len(await settled(runtime)) == 1
                runtime.pending.write_text(json.dumps({"title": "Must remain"}))
                await remote.close()
            runtime.version = version
            before = len([frame for frame in runtime.frames if frame["method"] == "tools/call"])
            with pytest.raises(MCPError, match="could not initialize"):
                await request(remote, "refused")
            assert len([frame for frame in runtime.frames if frame["method"] == "tools/call"]) == before
            assert runtime.status_reads == 1 + int(rollback)
            assert runtime.pending.exists() and runtime.decoy.exists()
        finally:
            await remote.close()
            owners.cancel_scope.cancel()


async def test_default_peer_does_not_require_or_add_bookmark_contract(bookmark_tcp):
    runtime = bookmark_tcp
    runtime.version = "missing"
    async with anyio.create_task_group() as owners:
        remote = peer(runtime, owners, pending=False)
        try:
            assert not (await request(remote)).get("isError")
            assert await settled(runtime) == []
            assert runtime.pending.exists() and runtime.decoy.exists()
            assert KEY not in next(frame for frame in runtime.frames if frame["method"] == "tools/call")["params"]["_meta"]
            assert remote.url == runtime.config.origin + "/mcp/v1"
            assert ("POST", "/mcp/v1") in runtime.exchanges
            assert all(path != "/mcp/v2" for _, path in runtime.exchanges)
        finally:
            await remote.close()
            owners.cancel_scope.cancel()


async def test_reserved_client_metadata_never_reaches_network(bookmark_tcp):
    runtime = bookmark_tcp
    async with anyio.create_task_group() as owners:
        remote = peer(runtime, owners)
        try:
            with pytest.raises(SharedPathError, match="reserved"):
                await request(remote, meta={KEY: {"version": 1, "path": str(runtime.decoy)}})
            assert runtime.status_reads == 0 and runtime.frames == []
            assert runtime.pending.exists() and runtime.decoy.exists()
        finally:
            await remote.close()
            owners.cancel_scope.cancel()


def test_nonmemory_peer_rejects_opt_in():
    with pytest.raises(ValueError, match="memory"):
        RemotePeer(None, SimpleNamespace(http=SimpleNamespace(url="http://127.0.0.1:1/mcp", service="health")),
                   lambda: {}, pending_file="/caller/.genesis/plan_bookmark_pending.json")


@pytest.mark.parametrize("era", ["2026-07-28", "2025-06-18"])
async def test_cached_generation_refuses_missing_bookmark_execution_route(bookmark_tcp, era):
    runtime = bookmark_tcp
    async with anyio.create_task_group() as owners:
        remote = peer(runtime, owners)
        try:
            if era == "2025-06-18":
                await remote.request(SimpleNamespace(request_id=0), "initialize", {
                    "protocolVersion": era, "capabilities": {},
                    "clientInfo": {"name": "fixture", "version": "1"}}, initialize_version=era)
                await remote.notify(None, "notifications/initialized", None)
            assert not (await request(remote, "before", era=era)).get("isError")
            assert len(await settled(runtime)) == 1
            generation = remote.current
            # Simulate loss of the execution contract without retiring the
            # logical peer or changing its cached feature/catalog preflight.
            assert any(route.path == "/mcp/v2" for route in runtime.app.router.routes)
            runtime.app.router.routes[:] = [route for route in runtime.app.router.routes if route.path != "/mcp/v2"]
            runtime.pending.write_text(json.dumps({"title": "Must remain after route loss"}))
            before = len([frame for frame in runtime.frames if frame["method"] == "tools/call"])
            with pytest.raises(MCPError):
                await request(remote, "after", era=era)
            calls = [frame for frame in runtime.frames if frame["method"] == "tools/call"]
            assert len(calls) == before + 1 and calls[-1]["id"] == "after"
            assert remote.current is generation and runtime.status_reads == 1
            assert len(await settled(runtime)) == 1 and runtime.pending.exists() and runtime.decoy.exists()
            assert all(path in {"/status", "/mcp/v2"} for _, path in runtime.exchanges)
        finally:
            await remote.close()
            owners.cancel_scope.cancel()
