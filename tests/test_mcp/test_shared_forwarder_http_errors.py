"""Actual SDK HTTP failure branches with an in-memory effect journal."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import json
from types import SimpleNamespace

import anyio
import httpx2
import pytest
from mcp.shared.exceptions import MCPError

from genesis.mcp.shared_client import _connection


async def exchange(monkeypatch, response, method="tools/call"):
    posts = []
    actual_client = httpx2.AsyncClient

    async def backend(request):
        # MockTransport cannot reach a real endpoint, even for a faulty redirect.
        assert request.url.host == "127.0.0.1" and request.method == "POST"
        body = json.loads(request.content)
        posts.append({"path": request.url.path, "body": body})
        if request.url.path == "/relocated":
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"fixture": 7}})
        return response(body)

    def client(*args, **kwargs):
        return actual_client(*args, **kwargs, transport=httpx2.MockTransport(backend))

    monkeypatch.setattr(_connection.httpx2, "AsyncClient", client)
    settings = SimpleNamespace(http=SimpleNamespace(url="http://127.0.0.1:1/mcp", service="health"), shutdown_grace_s=.25)
    async with anyio.create_task_group() as owners:
        peer = _connection.RemotePeer(owners, settings, lambda: {"Authorization": "Bearer fixture"})
        try:
            try:
                reply = await peer.request(SimpleNamespace(request_id="original"), method, {
                    "name": "fixture", "arguments": {},
                    "protocolVersion": "2026-07-28", "capabilities": {},
                    "clientInfo": {"name": "fixture", "version": "1"},
                    "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}})
            except MCPError as error:
                reply = error
        finally:
            await peer.close()
            owners.cancel_scope.cancel()
    return posts, reply


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("origin", ["same", "other"])
@pytest.mark.parametrize("method", ["tools/call", "tools/list", "initialize"])
async def test_redirect_never_resends_or_discloses_location(monkeypatch, status, origin, method):
    location = ("/relocated" if origin == "same" else "http://127.0.0.1:2/relocated") + "?fixture=PRIVATE"
    posts, error = await exchange(monkeypatch, lambda _: httpx2.Response(status, headers={"Location": location}), method)
    assert len(posts) == 1 and posts[0]["body"]["id"] == "original"
    assert isinstance(error, MCPError) and error.code == -32600
    assert error.message == ("Outcome unknown; not resent" if method == "tools/call"
                             else "Invalid shared MCP response")
    assert error.data == {"transport_error_code": -32600}


@pytest.mark.parametrize("kind", ["accepted", "content-type", "generic-500", "generic-401", "generic-403"])
@pytest.mark.parametrize("method", ["tools/call", "tools/list", "initialize"])
async def test_http_standin_is_safe_and_explicit(monkeypatch, kind, method):
    def response(_):
        status = 202 if kind == "accepted" else int(kind.removeprefix("generic-")) if kind.startswith("generic-") else 200
        return httpx2.Response(status, headers={"Content-Type": "text/PRIVATE"}, content=b"PRIVATE")

    posts, error = await exchange(monkeypatch, response, method)
    assert len(posts) == 1
    assert isinstance(error, MCPError)
    assert error.message == ("Outcome unknown; not resent" if method == "tools/call"
                             else "Invalid shared MCP response")
    assert error.data == {"transport_error_code": error.code}


@pytest.mark.parametrize("status", [400, 404, 500])
@pytest.mark.parametrize("method", ["tools/call", "tools/list", "initialize"])
@pytest.mark.parametrize("code", [-32600, -32601, -32602, -32700])
async def test_typed_business_error_keeps_code_message_and_data(monkeypatch, status, method, code):
    business = {"code": code, "message": "fixture business refusal", "data": {"fixture": 7}}
    posts, error = await exchange(monkeypatch, lambda body: httpx2.Response(
        status, json={"jsonrpc": "2.0", "id": body["id"], "error": business}), method)
    assert len(posts) == 1 and isinstance(error, MCPError)
    assert error.code == business["code"] and error.message == business["message"]
    assert error.data == business["data"]
