"""Connection generations; the SDK owns request tables and HTTP dispatch."""

import copy
import json
import logging
from dataclasses import dataclass, field

import anyio
import httpx2
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import MCP_DEFAULT_TIMEOUT
from mcp.shared.exceptions import MCPError
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp_types import CONNECTION_CLOSED, INTERNAL_ERROR, INVALID_REQUEST, PARSE_ERROR

from genesis.mcp.shared_paths import forwarder_url, prepare_params

logger = logging.getLogger(__name__)


def _connection_lost(error):
    # Qualified SDK transport sentinels: -32000 alone also means P4 busy.
    return error.code == CONNECTION_CLOSED and (error.message in {
        "Connection closed", "SSE stream ended without a response",
        "SSE stream ended and reconnection attempts were exhausted",
    } or error.message.startswith("SSE stream failed:"))


def _parser_failure(error):
    return error.code == PARSE_ERROR and error.message.startswith((
        "Failed to parse JSON response:", "Failed to parse SSE message:",
    ))


def _unknown_outcome(error):
    return (_connection_lost(error)
            or _parser_failure(error)
            or (error.code == INTERNAL_ERROR and error.message == "Server returned an error response")
            or (error.code == INVALID_REQUEST and (
                error.message == "server answered a request with 202 Accepted"
                or error.message.startswith(("Unexpected content type: ", "Redirect to "))
            )))


@dataclass(eq=False)
class _Generation:
    dispatcher: JSONRPCDispatcher
    stop: anyio.Event = field(default_factory=anyio.Event)
    done: anyio.Event = field(default_factory=anyio.Event)
    failed: bool = False
    session_id: str | None = None
    version: str | None = None
    session_missing: bool = False
    cleanup_attempted: bool = False


@dataclass
class _Negotiation:
    done: anyio.Event = field(default_factory=anyio.Event)
    succeeded: bool = False


class RemotePeer:
    def __init__(self, group, settings, header_factory, *, expected_catalog_revision=None):
        self.group, self.settings, self.header_factory = group, settings, header_factory
        self.url = forwarder_url(settings.http.url)
        self.current, self.front, self.initialization, self.version = None, None, None, None
        self.initialized, self.negotiation = False, None
        self.lock = anyio.Lock()
        self.expected_catalog_revision = expected_catalog_revision
        self.initialization_header_version = None

    def arm_initialize(self):
        if self.negotiation is not None and not self.negotiation.done.is_set():
            raise ValueError("Concurrent initialize is unavailable")
        self.negotiation = _Negotiation()

    async def _wait_initialize(self):
        gate = self.negotiation
        if gate is not None:
            await gate.done.wait()
            if not gate.succeeded:
                raise MCPError(INVALID_REQUEST, "Initialization did not complete")

    def _stamp(self, method, params, version=None, *, initialize_version=None):
        headers = {"mcp-method": method}
        params = params or {}
        if method == "tools/call" and isinstance(params.get("name"), str):
            headers["mcp-name"] = params["name"]
        meta = params.get("_meta")
        version = (meta.get("io.modelcontextprotocol/protocolVersion", version or self.version)
                   if isinstance(meta, dict) else version or self.version)
        if method == "initialize" and initialize_version is not None:
            version = initialize_version
        if isinstance(version, str):
            headers["mcp-protocol-version"] = version
        return headers

    async def _get(self):
        async with self.lock:
            if self.current is not None:
                if self.current.session_missing:
                    self.current.failed = True
                    self.current.stop.set()
                if not self.current.failed and not self.current.done.is_set():
                    return self._compatible(self.current)
                await self.current.done.wait()
            generation = await self.group.start(self._run_generation)
            self.current = generation
            return self._compatible(generation)

    def _compatible(self, generation):
        if self.version is not None and generation.version != self.version:
            generation.failed = True
            generation.stop.set()
            raise MCPError(INVALID_REQUEST, "Shared MCP protocol changed; restart this client")
        return generation

    async def _cleanup(self, generation, client):
        if generation.session_id and not generation.cleanup_attempted:
            generation.cleanup_attempted = True
            try:
                with anyio.fail_after(self.settings.shutdown_grace_s):
                    headers = {"mcp-session-id": generation.session_id}
                    if version := generation.version or self.version:
                        headers["mcp-protocol-version"] = version
                    response = await client.delete(self.url, headers=headers)
                    if response.status_code not in {200, 204, 404}:
                        logger.warning("Shared MCP session cleanup refused (%s)", response.status_code)
            except Exception as error:
                logger.warning("Shared MCP session cleanup unconfirmed (%s)", type(error).__name__)

    async def _run_generation(self, *, task_status=anyio.TASK_STATUS_IGNORED):
        generation, started = None, False
        headers = None

        async def observe_response(response):
            if generation is None:
                return
            method = response.request.method
            if (response.status_code == 404 and generation.session_id
                    and response.request.headers.get("mcp-session-id") == generation.session_id):
                try:
                    body = json.loads(await response.aread())
                    if body.get("error", {}).get("message") == "Session not found":
                        generation.session_missing = True
                except (ValueError, AttributeError, httpx2.StreamError):
                    pass
            if (method == "POST" and response.status_code < 400
                    and json.loads(response.request.content).get("method") == "initialize"):
                generation.session_id = response.headers.get("mcp-session-id")

        try:
            headers = self.header_factory()
            async with httpx2.AsyncClient(  # noqa: SIM117 — explicit transport/client ownership order
                headers=headers, trust_env=False, max_redirects=0,
                timeout=httpx2.Timeout(MCP_DEFAULT_TIMEOUT, read=None),
                event_hooks={"response": [observe_response]},
            ) as client:
                if self.expected_catalog_revision is not None:
                    response = await client.get(self.settings.http.origin + "/status")
                    response.raise_for_status()
                    status = response.json()
                    if (not isinstance(status, dict)
                            or status.get("service") != self.settings.http.service
                            or status.get("enabled") is not True
                            or type(status.get("transport_schema_version")) is not int
                            or status["transport_schema_version"] != 1
                            or status.get("catalog_revision") != self.expected_catalog_revision):
                        raise ValueError("Shared catalog does not match this frontend")
                async with streamable_http_client(
                    self.url, http_client=client,
                    terminate_on_close=False, max_sse_event_size=None,
                ) as (incoming, outgoing):
                    generation = _Generation(JSONRPCDispatcher(incoming, outgoing))
                    async with anyio.create_task_group() as active:
                        await active.start(generation.dispatcher.run, self.remote_request, self.remote_notify)
                        if self.initialization is not None:
                            request_id, params = self.initialization
                            reply = await generation.dispatcher.send_raw_request(
                                "initialize", copy.deepcopy(params),
                                {"request_id": request_id, "headers": self._stamp(
                                    "initialize", params, initialize_version=self.initialization_header_version),
                                 "cancel_on_abandon": False},
                            )
                            generation.version = reply.get("protocolVersion")
                            if self.initialized:
                                await generation.dispatcher.notify(
                                    "notifications/initialized", None,
                                    {"headers": self._stamp("notifications/initialized", None, generation.version)},
                                )
                        task_status.started(generation)
                        started = True
                        await generation.stop.wait()
                        active.cancel_scope.cancel()
                    # The HTTP writer must remain alive for queued courtesy cancels.
                    await self._cleanup(generation, client)
        except Exception as error:
            logger.warning("Shared MCP connection ended (%s)", type(error).__name__)
            if not started:
                raise MCPError(INVALID_REQUEST, "Shared MCP connection could not initialize") from None
        finally:
            if generation is not None:
                generation.failed = True
                with anyio.CancelScope(shield=True):
                    try:
                        if generation.session_id and not generation.cleanup_attempted:
                            async with httpx2.AsyncClient(
                                headers=headers, trust_env=False, timeout=None, max_redirects=0,
                            ) as client:
                                await self._cleanup(generation, client)
                    finally:
                        generation.done.set()

    async def request(self, context, method, params, *, initialize_version=None):
        generation, gate = None, self.negotiation if method == "initialize" else None
        try:
            params = prepare_params(self.settings.http.service, method, params)
            if method != "initialize":
                await self._wait_initialize()
            generation = await self._get()
            reply = await generation.dispatcher.send_raw_request(method, params, {
                "request_id": context.request_id, "headers": self._stamp(
                    method, params, initialize_version=initialize_version),
                "cancel_on_abandon": method != "initialize",
            })
            if method == "initialize":
                version = reply.get("protocolVersion")
                if not isinstance(version, str):
                    raise MCPError(INVALID_REQUEST, "Invalid shared MCP initialization")
                self.initialization = (context.request_id, copy.deepcopy(params))
                self.initialization_header_version = initialize_version
                self.version = generation.version = version
                if gate is not None:
                    gate.succeeded = True
            return reply
        except MCPError as error:
            if generation is not None and (_connection_lost(error) or generation.session_missing):
                generation.failed = True
                generation.stop.set()
            if _unknown_outcome(error):
                message = "Outcome unknown; not resent" if method == "tools/call" else "Invalid shared MCP response"
                raise MCPError(error.code, message,
                               data={"transport_error_code": error.code}) from None
            raise
        finally:
            if gate is not None:
                gate.done.set()

    async def notify(self, _context, method, params):
        if method == "notifications/cancelled":
            return  # SDK cancellation already cancels request() and emits its courtesy.
        await self._wait_initialize()
        if method == "notifications/initialized":
            self.initialized = True
        generation = await self._get()
        await generation.dispatcher.notify(method, params, {
            "headers": self._stamp(method, params, generation.version),
        })

    async def remote_request(self, context, method, params):
        return await self.front.send_raw_request(method, params, {"request_id": context.request_id})

    async def remote_notify(self, _context, method, params):
        if method != "notifications/cancelled":
            await self.front.notify(method, params)

    async def close(self):
        if self.current is not None:
            self.current.stop.set()
            await self.current.done.wait()
