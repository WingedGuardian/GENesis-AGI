"""Database binding before actual SDK memory effects and bookmark consumption."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import hashlib
import json
import os
from contextlib import asynccontextmanager

import pytest

from tests.test_mcp.test_plan_bookmark_http import (
    KEY,
    call,
    settled,
)
from tests.test_mcp.test_plan_bookmark_http import (
    bookmark_app as bookmark_app,
)
from tests.test_mcp.test_plan_bookmark_http import (
    bookmark_http as bookmark_http,
)
from tests.test_mcp.test_plan_bookmark_http import (
    database as database,
)
from tests.test_mcp.test_plan_bookmark_http import (
    memory_service as memory_service,
)
from tests.test_mcp.test_plan_bookmark_http import (
    runtime as runtime,
)
from tests.test_mcp.test_shared_http_runtime import unpack

TARGET_KEY = "genesis/databaseTarget"
TARGET_STATUS = "database_target_contract"
ENDPOINT = "/mcp/v3"


def target(runtime):
    path = runtime.db.path.resolve(strict=True)
    return {"version": 1, "target": hashlib.sha256(
        b"genesis-memory-sqlite-target-v1\0" + os.fsencode(path),
    ).hexdigest()}


async def test_memory_status_identifies_configured_database(bookmark_http):
    runtime = bookmark_http
    response = await runtime.client.get("/status")
    assert response.status_code == 200
    assert response.json().get(TARGET_STATUS) == target(runtime)
    assert str(runtime.db.path) not in response.text
    assert await settled(runtime) == []


async def test_bound_memory_route_preserves_the_bookmark_effect(bookmark_http):
    runtime = bookmark_http
    response = await call(runtime, endpoint=ENDPOINT, metadata={
        TARGET_KEY: target(runtime), KEY: {"version": 1, "path": str(runtime.pending)},
    })
    assert response.status_code == 200
    reply = unpack(response)
    assert reply["id"] == 3 and not reply["result"].get("isError")
    assert len(await settled(runtime)) == 1
    assert not runtime.pending.exists() and runtime.decoy.exists()


@pytest.mark.parametrize("value", [
    "absent", None, True, [], "target", {}, {"version": 1}, {"target": "0" * 64},
    {"version": True, "target": "match"}, {"version": 1.0, "target": "match"},
    {"version": "1", "target": "match"}, {"version": 2, "target": "match"},
    {"version": 1, "target": None}, {"version": 1, "target": 1},
    {"version": 1, "target": True}, {"version": 1, "target": ""},
    {"version": 1, "target": "0" * 64}, {"version": 1, "target": "g" * 64},
    {"version": 1, "target": "match", "extra": False},
])
async def test_invalid_target_precedes_checkout_and_bookmark(bookmark_http, monkeypatch, value):
    runtime = bookmark_http
    if isinstance(value, dict):
        value = {**value, **({"target": target(runtime)["target"]} if value.get("target") == "match" else {})}
    metadata = {KEY: {"version": 1, "path": str(runtime.pending)}}
    if value != "absent":
        metadata[TARGET_KEY] = value
    checkouts = []
    original = runtime.db.unit_of_work

    @asynccontextmanager
    async def counted():
        checkouts.append(True)
        async with original() as unit:
            yield unit

    with monkeypatch.context() as patch:
        patch.setattr(runtime.db, "unit_of_work", counted)
        response = await call(runtime, endpoint=ENDPOINT, metadata=metadata)
        assert checkouts == []
    assert response.status_code == 200
    reply = unpack(response)
    assert reply["result"].get("isError")
    assert str(runtime.db.path) not in response.text
    assert await settled(runtime) == []
    assert runtime.pending.exists() and runtime.decoy.exists()


@pytest.mark.parametrize("profile", ["external", "validator"])
async def test_bound_route_requires_unprofiled_forwarder(bookmark_http, profile):
    runtime = bookmark_http
    runtime.client.headers.pop("X-Genesis-Caller")
    response = await call(runtime, endpoint=ENDPOINT,
        headers={"Authorization": "Bearer " + profile[0] * 43}, metadata={
            TARGET_KEY: target(runtime),
        })
    assert response.status_code == 200 and unpack(response)["result"].get("isError")
    assert await settled(runtime) == []
    assert runtime.pending.exists() and runtime.decoy.exists()


@pytest.mark.parametrize("kind,code", [("auth", 401), ("host", 421), ("origin", 403), ("body", 413)])
async def test_bound_route_retains_outer_admission(bookmark_http, kind, code):
    runtime = bookmark_http
    headers = {"auth": {"Authorization": "Bearer invalid"}, "host": {"Host": "example.invalid"},
               "origin": {"Origin": "http://example.invalid"}, "body": {}}[kind]
    arguments = {"task_description": "x" * runtime.config.max_body_bytes} if kind == "body" else None
    response = await call(runtime, endpoint=ENDPOINT, headers=headers, arguments=arguments,
                          metadata={TARGET_KEY: target(runtime)})
    assert response.status_code == code
    assert await settled(runtime) == [] and runtime.pending.exists()


@pytest.mark.parametrize("identifier", [0, -3, "", "fixture", "123x"])
async def test_bound_route_preserves_exact_admitted_id(bookmark_http, identifier):
    runtime = bookmark_http
    response = await runtime.client.post(ENDPOINT,
        headers={"mcp-method": "tools/call", "mcp-name": "procedure_recall"},
        json={"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": {
            "name": "procedure_recall", "arguments": {"task_description": "synthetic-task"},
            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                      "io.modelcontextprotocol/clientCapabilities": {}, TARGET_KEY: target(runtime)}}})
    assert response.status_code == 200
    reply = unpack(response)
    assert not reply["result"].get("isError")
    assert type(reply["id"]) is type(identifier) and reply["id"] == identifier


@pytest.mark.parametrize("identifier", [True, 1.2, "1", "_GET_stream"])
async def test_bound_route_retains_raw_id_refusal(bookmark_http, identifier):
    runtime = bookmark_http
    response = await runtime.client.post(ENDPOINT,
        headers={"mcp-method": "tools/call", "mcp-name": "procedure_recall"},
        json={"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": {
            "name": "procedure_recall", "arguments": {"task_description": "synthetic-task"},
            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                      "io.modelcontextprotocol/clientCapabilities": {}, TARGET_KEY: target(runtime)}}})
    assert response.status_code == 400 and response.text == "Invalid MCP envelope"
    assert await settled(runtime) == [] and runtime.pending.exists()


@pytest.mark.parametrize("field", ["version", "target"])
async def test_duplicate_target_field_refuses_before_sdk_information_loss(bookmark_http, field):
    runtime = bookmark_http
    body = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "procedure_recall", "arguments": {"task_description": "synthetic-task"},
        "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                  "io.modelcontextprotocol/clientCapabilities": {}, TARGET_KEY: target(runtime),
                  KEY: {"version": 1, "path": str(runtime.pending)}}}}
    raw = json.dumps(body, separators=(",", ":"))
    anchor = '"version":1' if field == "version" else '"target":"' + target(runtime)["target"] + '"'
    invalid = '"version":false,' if field == "version" else '"target":false,'
    assert anchor in raw
    raw = raw.replace(anchor, invalid + anchor, 1)
    response = await runtime.client.post(ENDPOINT, content=raw,
        headers={"Content-Type": "application/json", "mcp-method": "tools/call", "mcp-name": "procedure_recall"})
    assert response.status_code == 400 and response.text == "Invalid MCP envelope"
    assert await settled(runtime) == []
    assert runtime.pending.exists() and runtime.decoy.exists()


async def test_health_does_not_advertise_or_serve_database_binding(runtime):
    response = await runtime.client.get("/status")
    assert response.status_code == 200 and TARGET_STATUS not in response.json()
    assert (await runtime.client.get(ENDPOINT)).status_code == 404
    assert runtime.state.calls == 0
