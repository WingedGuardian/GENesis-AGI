"""Characterize real registered tools independently of framework internals.

Catalog fixtures were captured before migrating the dependency. These calls
use real MCP dispatch with disposable DB/HTTP/service dependencies; they do not
bootstrap an install or contact a real provider.
"""

from __future__ import annotations

import importlib
import json
import sys
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

MODULES = {
    "health": "genesis.mcp.health_mcp",
    "memory": "genesis.mcp.memory_mcp",
    "outreach": "genesis.mcp.outreach_mcp",
    "recon": "genesis.mcp.recon_mcp",
    "discord-bot": "genesis.mcp.discord_bot_mcp",
    "peer": "scripts.genesis_peer_mcp",
}
CATALOG = Path(__file__).with_name("fixtures") / "mcp_catalogs.json"
V4_CATALOG = CATALOG.with_name("mcp_catalogs_v4.json")


def catalog_contract(tools):
    """Keep every advertised schema/description, excluding framework metadata."""
    # Framework meta namespaces and initialize version/capability fields change
    # across major versions; tool names, schemas and descriptions must not.
    fields = {"name", "description", "inputSchema", "outputSchema", "annotations", "title", "icons"}
    return sorted(
        ({key: value for key, value in tool.model_dump(by_alias=True, exclude_none=True).items()
          if key in fields} for tool in tools),
        key=lambda tool: tool["name"],
    )


@pytest.fixture(params=MODULES)
async def server_contract(request, monkeypatch, empty_db):
    name = request.param
    module = importlib.import_module(MODULES[name])
    if name == "health":
        monkeypatch.setattr(module, "_service", SimpleNamespace(snapshot=AsyncMock(return_value={})))
        monkeypatch.setattr(module, "_activity_tracker", None)
        tool, arguments = "health_status", {}
        expected = {
            "cc_sessions": {}, "infrastructure": {}, "queues": {}, "cost": {},
            "surplus": {}, "awareness": {}, "outreach_stats": {}, "services": {},
            "agent_api": {"status": "unknown"}, "provider_activity": [], "vcr": {}, "reflex": {},
        }
    elif name == "memory":
        monkeypatch.setattr(module, "_store", object())
        monkeypatch.setattr(module, "_retriever", object())
        monkeypatch.setattr(module, "_db", empty_db)
        monkeypatch.setattr(module, "_process_pending_bookmarks", False)
        tool, arguments, expected = "observation_query", {}, []
    elif name == "outreach":
        monkeypatch.setattr(module, "_db", empty_db)
        tool, arguments = "outreach_pending", {}
        expected = {"items": [], "total": 0, "offset": 0, "limit": 50, "truncated": False}
    elif name == "recon":
        monkeypatch.setattr(module, "_db", empty_db)
        tool, arguments, expected = "recon_findings", {}, []
    elif name == "discord-bot":
        monkeypatch.setattr(module, "_bot_token", "synthetic-test-token")
        # This module uses its own requests; the protocol Client below remains
        # the actual FastMCP client. No network socket can be opened by this fake.
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
        monkeypatch.setattr(module, "httpx", SimpleNamespace(
            AsyncClient=lambda **kwargs: real_client(transport=transport, **kwargs),
        ))
        tool, arguments = "fetch_messages", {"channel_id": "123"}
        expected = {"messages": [], "count": 0}
    else:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "fixture"}),
        )
        monkeypatch.setattr(module, "_client", lambda: httpx.AsyncClient(
            base_url="https://peer.example.invalid", transport=transport,
        ))
        tool, arguments, expected = "genesis_health", {}, {"status": "fixture"}
    return name, module.mcp, tool, arguments, expected


async def test_complete_server_catalog_matches_baseline(server_contract):
    name, server, *_ = server_contract
    # v4 advertises generated display titles, parsed parameter documentation,
    # and explicit extra-argument rejection. Freeze those exact deltas too.
    path = V4_CATALOG if int(version("fastmcp").split(".")[0]) >= 4 else CATALOG
    baseline = json.loads(path.read_text())
    async with Client(server) as client:
        actual = catalog_contract(await client.list_tools())
    assert actual == baseline[name]


def _schema_without_descriptions(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "description":
                continue
            if key in {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}:
                result[key] = {name: _schema_without_descriptions(schema)
                               for name, schema in item.items()}
            elif key in {"default", "const", "enum", "examples"}:
                result[key] = item
            else:
                result[key] = _schema_without_descriptions(item)
        return result
    if isinstance(value, list):
        return [_schema_without_descriptions(item) for item in value]
    return value


def test_schema_comparison_keeps_description_named_arguments_and_default_data():
    schema = {"properties": {"description": {"type": "string", "description": "help"}},
              "default": {"description": "default data"}}
    assert _schema_without_descriptions(schema) == {
        "properties": {"description": {"type": "string"}},
        "default": {"description": "default data"},
    }


@pytest.mark.parametrize("name", MODULES)
def test_catalog_schema_constraints_survive_upgrade(name):
    old = json.loads(CATALOG.read_text())[name]
    new = json.loads(V4_CATALOG.read_text())[name]
    assert len(old) == len(new) > 0
    for before, after in zip(old, new, strict=True):
        assert before["name"] == after["name"]
        assert after.pop("title") == before["name"].replace("_", " ").replace("-", " ").title()
        # Both frameworks reject unexpected keywords at invocation (covered
        # below); v4 additionally makes that existing constraint explicit.
        assert "additionalProperties" not in before["inputSchema"]
        assert after["inputSchema"].pop("additionalProperties") is False
        before.pop("description", None)
        after.pop("description", None)
        assert _schema_without_descriptions(before) == _schema_without_descriptions(after)


async def test_argument_coercion_and_unknown_keywords_keep_their_contract():
    server = FastMCP("validation-contract")

    @server.tool
    async def echo(value: int) -> int:
        return value

    async with Client(server) as client:
        assert (await client.call_tool("echo", {"value": "2"})).data == 2
        with pytest.raises(ToolError, match="Unexpected keyword argument"):
            await client.call_tool("echo", {"value": 1, "extra": "ignored"})


@pytest.mark.parametrize("module_path", MODULES.values())
async def test_synchronous_registry_matches_complete_public_local_listing(module_path):
    from genesis.cc.session_config import _registered_mcp_tool_names

    server = importlib.import_module(module_path).mcp
    public = {tool.name for tool in await server.local_provider.list_tools()}
    assert public
    # The helper is deliberately called while this event loop is running.
    assert set(_registered_mcp_tool_names(module_path)) == public


async def test_synchronous_security_registry_includes_hidden_writers(monkeypatch):
    from genesis.cc.session_config import _registered_mcp_tool_names

    server = FastMCP("hidden-writer-contract")

    @server.tool
    async def read() -> str:
        return "read"

    @server.tool
    async def write() -> str:
        return "write"

    @server.resource("fixture://data")
    async def data() -> str:
        return "resource"

    server.disable(names={"write"}, components={"tool"})
    monkeypatch.setitem(sys.modules, "_catalog_contract_fixture", SimpleNamespace(mcp=server))
    assert {tool.name for tool in await server.list_tools()} == {"read"}
    assert set(_registered_mcp_tool_names("_catalog_contract_fixture")) == {"read", "write"}


async def test_real_browser_wrapper_injects_context_and_delivers_progress(monkeypatch):
    from genesis.mcp.health import browser, mcp

    async def fill(selector, value):
        assert selector == "#fixture" and value == "fixture"
        report = browser._fill_progress.get()
        assert report is not None
        await report("fixture step")
        return {"success": True}

    events = []

    async def progress(value, total, message):
        events.append((value, total, message))

    monkeypatch.setattr(browser, "_impl_browser_fill", fill)
    monkeypatch.setattr(browser, "_FILL_PROGRESS_EVERY_S", 0)
    async with Client(mcp, mode="legacy") as client:
        result = await client.call_tool("browser_fill", {"selector": "#fixture", "value": "fixture"},
                                        progress_handler=progress)
    assert json.loads(result.content[0].text) == {"success": True}
    assert events == [(1.0, None, "fixture step")]


@pytest.mark.parametrize("components", [None, {}, {"malformed": object()}])
def test_synchronous_registry_refuses_unknown_or_empty_storage(monkeypatch, components):
    from genesis.cc.session_config import _registered_mcp_tool_names

    server = SimpleNamespace(local_provider=SimpleNamespace(_components=components))
    monkeypatch.setitem(sys.modules, "_catalog_contract_fixture", SimpleNamespace(mcp=server))
    with pytest.raises(RuntimeError, match="MCP"):
        _registered_mcp_tool_names("_catalog_contract_fixture")


async def test_representative_call_round_trips(server_contract):
    name, server, tool, arguments, expected = server_contract
    async with Client(server) as client:
        result = await client.call_tool(tool, arguments)
    assert not result.is_error
    # Compare the wire payload rather than generated client-side RootModel
    # wrappers, which are SDK implementation details and change in v2.
    if isinstance(expected, list):
        assert result.structured_content == {"result": expected}
        assert result.content == []
    else:
        assert len(result.content) == 1, (name, result.content)
        assert json.loads(result.content[0].text) == expected
