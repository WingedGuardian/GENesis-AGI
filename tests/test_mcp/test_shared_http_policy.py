"""Closed credential and defining-function policy on actual upstream objects."""

import json

import pytest
from fastmcp import FastMCP
from fastmcp.tools import Tool
from starlette.requests import Request

from genesis.caller_context import CallerContext
from genesis.caller_credentials import CallerResolver
from genesis.mcp.shared_http import _auth
from genesis.mcp.shared_http._catalog import Catalog

FIELDS = {"dispatched": True, "supervised": False, "session_id": "fixture-slot", "origin": None,
          "transcript_id": "fixture-transcript", "legacy_session_id": "fixture-legacy"}


def auth_request(monkeypatch, headers):
    request = Request({"type": "http", "method": "POST", "path": "/mcp", "headers": headers})
    monkeypatch.setattr(_auth, "get_http_request", lambda: request)


@pytest.mark.parametrize("value", ["{", "null", "[]", "false", "1", '"string"', "{}",
    '{"dispatched":true,"dispatched":false}', '{"dispatched":NaN}', '{"origin":"spoof"}',
    "[" * 1500, " " * 4097, '{"origin":"\u00e9"}'])
async def test_malformed_forwarded_identity_refuses_token(monkeypatch, value):
    auth_request(monkeypatch, [(b"authorization", b"Bearer " + b"f" * 43),
                              (b"x-genesis-caller", value.encode("latin-1"))])
    auth = _auth.CallerAuth(CallerResolver("f" * 43, {}), "health")
    assert await auth.verify_token("f" * 43) is None


@pytest.mark.parametrize("header", [b"authorization", b"x-genesis-caller"])
async def test_duplicate_authority_header_refused(monkeypatch, header):
    values = {b"authorization": b"Bearer " + b"f" * 43,
              b"x-genesis-caller": json.dumps(FIELDS).encode()}
    auth_request(monkeypatch, [*values.items(), (header, values[header])])
    assert await _auth.CallerAuth(CallerResolver("f" * 43, {}), "health").verify_token("f" * 43) is None


@pytest.mark.parametrize("field,value", [("dispatched", False), ("supervised", True), ("session_id", "other"),
    ("origin", "external_untrusted"), ("transcript_id", "other"), ("legacy_session_id", "other")])
async def test_complete_caller_changes_sdk_principal(monkeypatch, field, value):
    auth = _auth.CallerAuth(CallerResolver("f" * 43, {}), "health")
    tokens = []
    for fields in (FIELDS, {**FIELDS, field: value}):
        auth_request(monkeypatch, [(b"authorization", b"Bearer " + b"f" * 43),
                                  (b"x-genesis-caller", json.dumps(fields).encode())])
        tokens.append(await auth.verify_token("f" * 43))
    assert tokens[0].subject != tokens[1].subject
    assert isinstance(tokens[0].claims["genesis_caller"], CallerContext)


@pytest.mark.parametrize("service", ["health", "memory", "outreach", "recon"])
@pytest.mark.parametrize("profile", ["external", "validator", "interactive"])
async def test_profile_service_cross_product(monkeypatch, service, profile):
    auth_request(monkeypatch, [(b"authorization", b"Bearer " + b"e" * 43)])
    resolver = CallerResolver(None, {profile: "e" * 43})
    token = await _auth.CallerAuth(resolver, service).verify_token("e" * 43)
    assert (token is not None) is (service in {"health", "memory"} and profile != "interactive")
    if token is not None:
        assert token.claims["genesis_caller"].profile == profile


@pytest.mark.parametrize("service", ["health", "memory", "outreach", "recon"])
def test_empty_or_unusable_credentials_fail_preflight(service):
    with pytest.raises(ValueError):
        _auth.usable_credentials(CallerResolver(None, {}), service)
    with pytest.raises(ValueError):
        _auth.usable_credentials(CallerResolver(None, {"interactive": "i" * 43}), service)
    _auth.usable_credentials(CallerResolver("f" * 43, {}), service)


def function(module):
    async def fixture():
        return "fixture"
    fixture.__module__ = module
    return fixture


@pytest.mark.parametrize("module,local", [
    ("genesis.mcp.health.status", False), ("genesis.mcp.health.browser", True),
    ("genesis.mcp.health.module_ops", True), ("genesis.mcp.health.payment_context", True),
    ("genesis.browser.child", True), ("genesis.modules", True), ("genesis.payments.provider", True),
])
async def test_local_parent_cannot_be_laundered_by_profile_allowed_alias(module, local):
    server = FastMCP("catalog-fixture")
    original = Tool.from_function(function(module), name="health_status")
    server.add_tool(Tool.from_tool(original, name="provider_activity"))
    catalog = await Catalog.capture(server, "health")
    for caller in (CallerContext(), CallerContext(profile="external"), CallerContext(profile="validator")):
        assert catalog.allowed("provider_activity", caller) is not local
        assert not catalog.allowed("health_status", caller)
        assert not catalog.allowed("hash_provider_activity", caller)


async def test_profile_requires_every_parent_capability_and_digest_matches_visibility():
    server = FastMCP("catalog-fixture")
    original = Tool.from_function(function("genesis.mcp.health.status"), name="private_fixture")
    server.add_tool(Tool.from_tool(original, name="health_status"))
    catalog = await Catalog.capture(server, "health")
    forwarder, external = CallerContext(), CallerContext(profile="external")
    assert catalog.allowed("health_status", forwarder)
    assert not catalog.allowed("health_status", external)
    assert catalog.revision(forwarder) != catalog.revision(external)
    assert catalog.revision(forwarder, enabled=False) == catalog.revision(external)


@pytest.mark.parametrize("module", ["unqualified.module", "genesis.browserlike", "genesis.mcp.health.new_tools"])
async def test_unknown_defining_modules_refuse_startup(module):
    server = FastMCP("catalog-fixture")
    server.tool(function(module), name="health_status")
    with pytest.raises(ValueError, match="defining module"):
        await Catalog.capture(server, "health")
