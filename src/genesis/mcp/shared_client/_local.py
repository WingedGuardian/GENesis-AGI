"""One SDK connection owns the ordinary frontend; middleware selects execution."""

import copy
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

import anyio
import mcp_types as types
from fastmcp.client.transports.base import TransportOptions
from fastmcp.client.transports.memory import FastMCPTransport
from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult
from mcp import ClientSession
from mcp.shared.exceptions import MCPError
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, LATEST_HANDSHAKE_VERSION

from genesis.caller_context import caller_context
from genesis.mcp.external_profiles import ExternalProfileMiddleware, profile_tools
from genesis.mcp.shared_http._catalog import Catalog
from genesis.mcp.shared_settings import LOCAL_FRONTEND_STAGES

from ._connection import RemotePeer

_FRAME: ContextVar = ContextVar("local_mcp_original_frame", default=None)


@dataclass(frozen=True)
class _Frame:
    request_id: int | str | None
    method: str
    params: dict | None


def _original_frame(method):
    frame = _FRAME.get()
    if frame is None or frame.method != method:
        raise MCPError(types.INVALID_REQUEST, "Original frontend frame unavailable")
    return frame


class _Routes(Middleware):
    def __init__(self, peer, catalog):
        self.peer, self.catalog = peer, catalog
        floor = profile_tools(catalog.service, "external")
        self.remote_names = frozenset(
            name for name, entry in catalog.entries.items()
            if entry.location == "shared" and entry.capabilities <= floor
        )
        if self.remote_names != floor:
            raise ValueError(f"Incomplete {catalog.service} read frontend")

    async def on_initialize(self, context, call_next):
        if context.message is None:
            return await call_next(context)  # SDK owns malformed initialization.
        frame = _original_frame("initialize")
        requested = context.message.params.protocol_version
        version = requested if requested in HANDSHAKE_PROTOCOL_VERSIONS else LATEST_HANDSHAKE_VERSION
        self.peer.remote.arm_initialize()
        # The SDK treats initialize as legacy even when its metadata is modern.
        # Select that HTTP era while retaining the native ID/body verbatim.
        await self.peer.remote.request(frame, frame.method, frame.params, initialize_version=version)
        # FastMCP's compatibility hook swallows an MCPError AFTER call_next.
        # Veto before it: the SDK commits local state only on chain success.
        return await call_next(context)

    async def on_notification(self, context, call_next):
        result = await call_next(context)
        if context.method != "notifications/cancelled":
            frame = _original_frame(context.method)
            await self.peer.remote.notify(frame, frame.method, frame.params)
        return result

    async def on_call_tool(self, context, call_next):
        name = context.message.name
        if name not in self.catalog.entries:
            # Prevent SDK hidden hash lookup from bypassing the frozen route.
            return ToolResult(content=[types.TextContent(type="text", text=f"Unknown tool: {name!r}")],
                              is_error=True)
        if name not in self.remote_names:
            return await call_next(context)
        frame = _original_frame("tools/call")
        reply = await self.peer.remote.request(frame, frame.method, frame.params)
        return ToolResult.from_mcp_result(types.CallToolResult.model_validate(reply))


class _LocalPeer:
    def __init__(self, remote):
        self.remote, self.group = remote, remote.group
        self.front, self.local, self.negotiation = None, None, None

    def arm_initialize(self):
        if self.negotiation is not None and not self.negotiation.is_set():
            raise ValueError("Concurrent initialize is unavailable")
        self.negotiation = anyio.Event()

    async def _wait_initialize(self):
        if self.negotiation is not None:
            await self.negotiation.wait()

    async def request(self, context, method, params):
        gate = self.negotiation if method == "initialize" else None
        if method != "initialize":
            await self._wait_initialize()
        frame = _Frame(context.request_id, method, copy.deepcopy(params))
        token = _FRAME.set(frame)
        try:
            return await self.local.send_raw_request(method, params, {
                "request_id": context.request_id, "cancel_on_abandon": method != "initialize",
            })
        finally:
            _FRAME.reset(token)
            if gate is not None:
                gate.set()  # The SDK owns accepted/rejected protocol state.

    async def notify(self, context, method, params):
        if method == "notifications/cancelled":
            return  # Front cancellation emits the selected SDK courtesy chain.
        await self._wait_initialize()
        token = _FRAME.set(_Frame(None, method, copy.deepcopy(params)))
        try:
            await self.local.notify(method, params)
        finally:
            _FRAME.reset(token)

    async def remote_request(self, context, method, params):
        return await self.front.send_raw_request(method, params, {"request_id": context.request_id})

    async def remote_notify(self, context, method, params):
        await self.front.notify(method, params)

    async def close(self):
        await self.remote.close()


@asynccontextmanager
async def local_peer(group, settings, header_factory, server, stage):
    caller = caller_context()
    service = settings.http.service
    if ((service, stage) not in LOCAL_FRONTEND_STAGES or caller.profile is not None
            or any(isinstance(m, ExternalProfileMiddleware) for m in server.middleware)):
        raise ValueError("Unsupported local frontend")
    catalog = await Catalog.capture(server, service)
    remote = RemotePeer(group, settings, header_factory, expected_catalog_revision=catalog.revision(caller),
                        pending_file=Path.home() / ".genesis/plan_bookmark_pending.json" if service == "memory" else None)
    peer = _LocalPeer(remote)
    routes = _Routes(peer, catalog)

    class RawSession(ClientSession):
        def __init__(self, read_stream, write_stream, **kwargs):
            class Relay(JSONRPCDispatcher):
                async def run(self, _request, _notify, _intercept=None, *, task_status=anyio.TASK_STATUS_IGNORED):
                    await super().run(peer.remote_request, peer.remote_notify, task_status=task_status)

            self.relay = Relay(read_stream, write_stream)
            super().__init__(dispatcher=self.relay, **kwargs)

    server.middleware.insert(0, routes)
    try:
        async with FastMCPTransport(server).connect_session(
            transport_options=TransportOptions(session_class=RawSession),
        ) as session:
            peer.local = session.relay
            yield peer
    finally:
        server.middleware.remove(routes)
