"""Ordinary SDK oracle versus the actual mixed peer, with a synthetic HTTP leg."""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import mcp_types as types
from fastmcp import FastMCP
from fastmcp.client.transports.base import TransportOptions
from fastmcp.client.transports.memory import FastMCPTransport
from fastmcp.server.dependencies import get_context
from fastmcp.server.middleware import Middleware
from mcp import ClientSession
from mcp.shared.exceptions import MCPError
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.shared.memory import create_client_server_memory_streams
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, LATEST_HANDSHAKE_VERSION

from genesis.caller_context import CallerContext, request_caller
from genesis.mcp import health
from genesis.mcp.health import browser, status
from genesis.mcp.shared_client import _local

MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
          "io.modelcontextprotocol/clientCapabilities": {}, "fixture.local": {"kept": True}}
INIT = ("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "fixture", "version": "1"}})


def call(name, modern=True):
    return "tools/call", {"name": name, "arguments": {"selector": "#synthetic", "value": "fixture"}
                           if name == "browser_fill" else {}, **({"_meta": MODERN} if modern else {})}


SEQUENCES = {
    "modern-local-unstamped-remote": [call("browser_fill"), call("health_status", False)],
    "modern-remote-unstamped-local": [call("health_status"), call("browser_fill", False)],
    "modern-local-legacy-init": [call("browser_fill"), INIT, call("health_status")],
    "modern-remote-legacy-init": [call("health_status"), INIT, call("browser_fill")],
    "legacy-modern-local": [INIT, call("browser_fill")],
    "legacy-modern-remote": [INIT, call("health_status")],
    "valid-modern": [call("browser_fill"), call("health_status")],
    "valid-legacy": [INIT, call("browser_fill", False), call("health_status", False)],
    "failed-init": [INIT, call("browser_fill", False), call("health_status", False)],
    "stamped-modern-init": [("initialize", {**INIT[1], "_meta": MODERN}), call("browser_fill", False)],
    "remote-wire-error": [call("health_status")],
    "remote-unknown-outcome": [call("health_status")],
    "remote-error-result": [call("health_status")],
    "unknown-name": [call("fixture_unknown")],
    "hidden-hash": [call("f" * 12 + "_health_status")],
}


async def _run(monkeypatch, sequence, id_kind, arm, fault):
    effects, requests, notifications, closed = [], [], [], []

    def receipt(name):
        rc = get_context().request_context
        effects.append({"name": name, "id": rc._srctx.request_id, "owner": "local"})

    def payload():
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="fixture complete"),
                     types.ImageContent(type="image", data="AA==", mime_type="image/png")],
            structured_content={"fixture": "complete"}, meta={"fixture.result": {"kept": True}},
            is_error=sequence == "remote-error-result",
        )

    async def fill(selector, value):
        receipt("browser_fill")
        return {"filled": selector, "value": value}

    async def health_status():
        receipt("health_status")
        return payload()

    monkeypatch.setattr(browser, "_impl_browser_fill", fill)
    monkeypatch.setattr(status, "_impl_health_status", health_status)

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield {}
        finally:
            await anyio.lowlevel.checkpoint()
            closed.append("returned")

    server = FastMCP("genesis-health", lifespan=lifespan)
    for tool in await health.mcp.list_tools(run_middleware=False):
        if sequence == "hidden-hash" and tool.name == "health_status":
            tool = tool.model_copy(update={"meta": {
                "fastmcp": {"tool_hash": "f" * 12}, "ui": {"visibility": ["model", "app"]},
            }})
        server.add_tool(tool)

    class RejectInitialize(Middleware):
        async def on_initialize(self, context, call_next):
            raise MCPError(-32000, "fixture initialization refused", data={"fixture": "kept"})

    if arm == "oracle" and sequence == "failed-init":
        server.add_middleware(RejectInitialize())

    def remote_error():
        if sequence == "remote-wire-error":
            return MCPError(-32000, "fixture busy", data={"fixture": "kept"})
        return MCPError(-32700, "Outcome unknown; not resent", data={"transport_error_code": -32700})

    class RemoteFailure(Middleware):
        async def on_call_tool(self, context, call_next):
            if sequence == "remote-unknown-outcome":
                receipt("health_status")  # The independently observed effect precedes the lost reply.
            raise remote_error()

    if arm == "oracle" and sequence in {"remote-wire-error", "remote-unknown-outcome"}:
        server.add_middleware(RemoteFailure())

    class Remote:
        def __init__(self, group, settings, header_factory, **kwargs):
            self.group, self.version, self.front = group, None, None
            assert len(kwargs["expected_catalog_revision"]) == 64

        def arm_initialize(self):
            pass

        async def request(self, context, method, params, *, initialize_version=None):
            requests.append({"id": context.request_id, "method": method, "params": copy.deepcopy(params)})
            if method == "initialize":
                if sequence == "failed-init":
                    raise MCPError(-32000, "fixture initialization refused", data={"fixture": "kept"})
                version = params["protocolVersion"]
                self.version = version if version in HANDSHAKE_PROTOCOL_VERSIONS else LATEST_HANDSHAKE_VERSION
                assert initialize_version == self.version
                return {"protocolVersion": self.version}
            assert method == "tools/call" and params["name"] == "health_status"
            if sequence == "remote-wire-error":
                raise remote_error()
            effects.append({"name": "health_status", "id": context.request_id, "owner": "remote"})
            if sequence == "remote-unknown-outcome":
                raise remote_error()
            return payload().model_dump(mode="json", by_alias=True, exclude_none=True)

        async def notify(self, context, method, params):
            pass

        async def close(self):
            pass

    async def notify(context, method, params):
        notifications.append({"method": method, "params": params})

    async def unused(*args):
        return {}

    class RawSession(ClientSession):
        def __init__(self, read_stream, write_stream, **kwargs):
            self.relay = JSONRPCDispatcher(read_stream, write_stream)
            super().__init__(dispatcher=self.relay, **kwargs)

    responses = []
    with request_caller(CallerContext()), anyio.fail_after(15):
        if arm == "oracle":
            async with FastMCPTransport(server).connect_session(
                transport_options=TransportOptions(session_class=RawSession),
            ) as session:
                for i, (method, params) in enumerate(SEQUENCES[sequence]):
                    request_id = i + 3 if id_kind == "int" else "native-" + str(i)
                    try:
                        result = await session.relay.send_raw_request(method, params, {"request_id": request_id})
                        responses.append({"result": result})
                    except MCPError as error:
                        responses.append({"error": {"code": error.code, "message": error.message, "data": error.data}})
                    if method == "initialize" and "result" in responses[-1]:
                        await session.relay.notify("notifications/initialized", None)
        else:
            monkeypatch.setattr(_local, "RemotePeer", Remote)
            settings = SimpleNamespace(http=SimpleNamespace(service="health"))
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                native, front = JSONRPCDispatcher(*client_streams), JSONRPCDispatcher(*server_streams)
                async with anyio.create_task_group() as group:
                    async with _local.local_peer(group, settings, lambda: {}, server, "health-reads") as peer:
                        peer.front = peer.remote.front = front
                        stopped = anyio.Event()

                        async def drive(*, task_status=anyio.TASK_STATUS_IGNORED):
                            try:
                                await front.run(peer.request, peer.notify, task_status=task_status)
                            finally:
                                stopped.set()

                        await group.start(native.run, unused, notify)
                        await group.start(drive)
                        if fault == "route-locally":
                            server.middleware[0].remote_names = frozenset()
                        for i, (method, params) in enumerate(SEQUENCES[sequence]):
                            request_id = i + 3 if id_kind == "int" else "native-" + str(i)
                            if method == "initialize":
                                peer.arm_initialize()
                            try:
                                result = await native.send_raw_request(method, params, {"request_id": request_id})
                                responses.append({"result": result})
                            except MCPError as error:
                                responses.append({"error": {"code": error.code, "message": error.message, "data": error.data}})
                            if method == "initialize" and "result" in responses[-1]:
                                await native.notify("notifications/initialized", None)
                        await client_streams[1].aclose()
                        await stopped.wait()
                    group.cancel_scope.cancel()
    assert closed == ["returned"]
    return {"responses": responses, "effects": effects, "requests": requests}


async def compare(monkeypatch, sequence, id_kind, *, fault=None):
    return [await _run(monkeypatch, sequence, id_kind, arm, fault) for arm in ("oracle", "mixed")]
