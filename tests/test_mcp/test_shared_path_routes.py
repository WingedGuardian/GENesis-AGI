"""Both routes retain one admission/authentication/session owner."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import json

import pytest

from tests.test_mcp.test_shared_http_runtime import runtime as runtime
from tests.test_mcp.test_shared_http_runtime import unpack

META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {}}


async def call(runtime, endpoint, identifier=7, *, name="health_status", arguments=None, **options):
    return await runtime.client.post(endpoint, headers={"mcp-method": "tools/call", "mcp-name": name},
        json={"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": {
            "name": name, "arguments": arguments or {}, "_meta": META}}, **options)


@pytest.mark.parametrize("endpoint", ["/mcp", "/mcp/v1"])
@pytest.mark.parametrize("body", [
    b'[]', b'null', b'{', b'{}',
    b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}',
    b'{"jsonrpc":"2.0","id":"1","method":"ping"}',
    b'{"jsonrpc":"2.0","id":true,"method":"ping"}',
    b'{"jsonrpc":"2.0","id":1.5,"method":"ping"}',
    b'{"jsonrpc":"2.0","id":"_GET_stream","method":"ping"}',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"health_status","task":{"ttl":1000}}}',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"health_status","_meta":{"x":1,"x":2}}}',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"health_status","arguments":{"x":NaN}}}',
    json.dumps({"jsonrpc": "2.0", "id": "9" * 3000, "method": "ping"}).encode(),
])
async def test_both_routes_refuse_raw_ambiguity_before_body(runtime, endpoint, body):
    headers = {"Content-Type": "application/json"}
    if b'"task"' in body:
        # Make this frame otherwise valid: a missing protocol envelope would
        # let the SDK reject it even if the alias's raw guard were removed.
        request = json.loads(body)
        request["params"]["_meta"] = META
        body = json.dumps(request).encode()
        headers.update({"mcp-method": "tools/call", "mcp-name": "health_status"})
    response = await runtime.client.post(endpoint, content=body, headers=headers)
    assert response.status_code == 400
    assert runtime.state.calls == 0


@pytest.mark.parametrize("endpoint", ["/mcp", "/mcp/v1"])
@pytest.mark.parametrize("identifier", [0, -3, 3, "", "fixture", "123x", "_GET_streamx", "9" * 3000 + "x"],
                         ids=["zero", "negative", "integer", "empty", "text", "mixed", "sentinel-prefix", "long-mixed"])
async def test_both_routes_preserve_admitted_ids_and_actual_body(runtime, endpoint, identifier):
    response = await call(runtime, endpoint, identifier)
    reply = unpack(response)
    assert response.status_code == 200 and not reply["result"].get("isError")
    assert type(reply["id"]) is type(identifier) and reply["id"] == identifier
    assert runtime.state.calls == 1


@pytest.mark.parametrize("endpoint", ["/mcp", "/mcp/v1"])
async def test_both_routes_auth_authority_body_and_catalog_remain_closed(runtime, endpoint):
    for verb in ("GET", "POST", "DELETE"):
        options = {"content": json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"_meta": META}})} if verb == "POST" else {}
        headers = {"Content-Type": "application/json", "mcp-method": "ping"} if verb == "POST" else {}
        invalid = await runtime.client.request(verb, endpoint, headers={**headers, "Authorization": "Bearer invalid"}, **options)
        assert invalid.status_code == 401, (verb, invalid.text)
        authority = await runtime.client.request(verb, endpoint, headers={**headers, "Host": "127.0.0.1:9124"}, **options)
        assert authority.status_code == 421
        origin = await runtime.client.request(verb, endpoint, headers={**headers, "Origin": "http://untrusted.invalid"}, **options)
        assert origin.status_code == 403
    for body in (b"invalid-json", b"x" * 5000):
        invalid = await runtime.client.post(endpoint, content=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer invalid"})
        assert invalid.status_code == 401
    oversized = await runtime.client.post(endpoint, content=b"x" * 5000,
                                         headers={"Content-Type": "application/json"})
    assert oversized.status_code == 413
    for name in ("private_fixture", "local_fixture", "unknown"):
        reply = unpack(await call(runtime, endpoint, name=name))
        assert reply.get("error") or reply["result"].get("isError")
    assert runtime.state.calls == 0
    runtime.state.enabled = False
    reply = unpack(await call(runtime, endpoint))
    assert reply.get("error") or reply["result"].get("isError")
    assert runtime.state.calls == 0


@pytest.mark.parametrize("endpoint", ["/mcp", "/mcp/v1"])
async def test_valid_large_id_still_obeys_configured_body_limit(runtime, endpoint):
    response = await call(runtime, endpoint, "9" * 5000)
    assert response.status_code == 413 and runtime.state.calls == 0


async def test_alias_shares_session_limit_principal_and_authenticated_cleanup(runtime):
    async def initialize(endpoint):
        return await runtime.client.post(endpoint, headers={"MCP-Protocol-Version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "fixture", "version": "1"}}})

    first = await initialize("/mcp")
    assert first.status_code == 200
    assert (await initialize("/mcp/v1")).status_code == 200
    assert (await initialize("/mcp/v1")).status_code == 503
    headers = {"MCP-Protocol-Version": "2025-11-25", "Mcp-Session-Id": first.headers["mcp-session-id"]}
    refused = await runtime.client.delete("/mcp/v1", headers={**headers, "Authorization": "Bearer " + "v" * 43})
    assert refused.status_code == 404
    assert (await runtime.client.delete("/mcp/v1", headers=headers)).status_code == 200
    assert (await initialize("/mcp")).status_code == 200
