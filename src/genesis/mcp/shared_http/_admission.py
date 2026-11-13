"""Exact authority and raw IDs, without replacing SDK protocol dispatch."""

import json

from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from mcp.shared.dispatcher import as_request_id, coerce_request_id
from mcp_types import jsonrpc_message_adapter
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response


class _Object(dict):
    def __init__(self, pairs):
        super().__init__(pairs)
        self.duplicate = len(pairs) != len(self)


def _not_json_constant(_value):
    raise ValueError("Invalid JSON constant")


def _check_id(value):
    if isinstance(value, str) and (value == "_GET_stream" or type(coerce_request_id(value)) is int):
        raise ValueError("Unsupported shared request ID")


def validate_envelope(body: bytes) -> dict:
    try:
        value = json.loads(body, object_pairs_hook=_Object, parse_constant=_not_json_constant)
        if not isinstance(value, dict):
            raise ValueError("Invalid shared envelope")
        params = value.get("params")
        # Disabled task support does not itself refuse augmentation in this
        # qualified stack: the adapter otherwise runs the ordinary tool body.
        if value.get("method") == "tools/call" and isinstance(params, dict) and params.get("task") is not None:
            raise ValueError("Shared task augmentation is unavailable")
        structural = [value, value.get("error")]
        if isinstance(params, dict):
            structural.extend((params, params.get("_meta")))
        if any(isinstance(node, _Object) and node.duplicate for node in structural):
            raise ValueError("Duplicate envelope field")
        if "id" in value:
            if value["id"] is not None and as_request_id(value["id"]) is None:
                raise ValueError("Invalid shared request ID")
            _check_id(value["id"])
        if value.get("method") == "notifications/cancelled" and isinstance(params, dict):
            # The actual dispatcher ignores invalid shapes and the request_id alias.
            acting_id = as_request_id(params.get("requestId"))
            if acting_id is not None:
                _check_id(acting_id)
        jsonrpc_message_adapter.validate_python(value, by_name=False)
        return value
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("Invalid shared envelope") from exc


class ExactAuthority:
    def __init__(self, app, *, authority: str, origin: str):
        self.app = app
        self.validator = TransportSecurityMiddleware(TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=[authority], allowed_origins=[origin],
        ))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        hosts, origins = request.headers.getlist("host"), request.headers.getlist("origin")
        if len(hosts) != 1 or len(origins) > 1 or (origins and not origins[0]):
            return await Response("Invalid authority headers", status_code=400)(scope, receive, send)
        response = await self.validator.validate_request(request, is_post=scope["method"] == "POST")
        await (response or self.app)(scope, receive, send)


class EnvelopeGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/mcp":
            return await self.app(scope, receive, send)
        try:
            body = await Request(scope, receive).body()
            validate_envelope(body)
        except (ValueError, ClientDisconnect):
            return await Response("Invalid MCP envelope", status_code=400)(scope, receive, send)
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
