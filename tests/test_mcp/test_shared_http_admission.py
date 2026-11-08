"""Qualified shared-only envelope/URL limits, before MCP dispatch."""

import json

import pytest

from genesis.mcp.shared_http._admission import EnvelopeGuard, validate_envelope
from genesis.mcp.shared_http._settings import SharedHTTPConfig, require_qualified_stack


def settings(**overrides):
    return SharedHTTPConfig(**{
        "service": "health", "url": "http://127.0.0.1:8100/mcp", "db_units": 2,
        "tool_concurrency": 2, "max_sessions": 3, "session_idle_timeout_s": 30,
        "max_body_bytes": 4096, **overrides,
    })


@pytest.mark.parametrize("url,authority", [
    ("http://127.0.0.1:8100/mcp", "127.0.0.1:8100"),
    ("http://127.1.2.3:8100/mcp", "127.1.2.3:8100"),
    ("http://[::1]:8100/mcp", "[::1]:8100"),
    ("http://[0:0:0:0:0:0:0:1]:8100/mcp", "[0:0:0:0:0:0:0:1]:8100"),
])
def test_exact_configured_loopback_authority(url, authority):
    config = settings(url=url)
    assert config.authority == authority
    assert config.origin == "http://" + authority


@pytest.mark.parametrize("url", [
    "http://localhost:8100/mcp", "http://192.0.2.1:8100/mcp", "http://0.0.0.0:8100/mcp",
    "https://127.0.0.1:8100/mcp", "http://127.0.0.1/mcp", "http://127.0.0.1:0/mcp",
    "http://127.0.0.1:65536/mcp", "http://user@127.0.0.1:8100/mcp",
    "http://127.0.0.1:8100/mcp?x=1", "http://127.0.0.1:8100/mcp#x",
    " http://127.0.0.1:8100/mcp", "http://127.0.0.1:8100/mcp\n",
    "http://[::1%lo]:8100/mcp", "http://127.0.0.1:8100/mcp/", "http://127.0.0.1:8100//mcp",
    "http://127.0.0.1:*/mcp", "http://0177.0.0.1:8100/mcp", "http://127.0.0.1:/mcp",
])
def test_invalid_bind_configuration_refuses(url):
    with pytest.raises(ValueError):
        settings(url=url)


@pytest.mark.parametrize("field", ["db_units", "tool_concurrency", "max_sessions", "max_body_bytes"])
@pytest.mark.parametrize("value", [0, -1, True, 1.2, "2", None])
def test_count_limits_are_explicit_positive_integers(field, value):
    with pytest.raises(ValueError):
        settings(**{field: value})


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), "2", None])
def test_idle_limit_is_finite_positive_and_not_bool(value):
    with pytest.raises(ValueError):
        settings(session_idle_timeout_s=value)


def test_concurrency_cannot_exceed_foreground_budget():
    with pytest.raises(ValueError):
        settings(tool_concurrency=3)
    with pytest.raises(ValueError):
        settings(service="discord-bot")


def test_version_fence_is_exact_and_missing_is_not_qualified(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    from genesis.mcp.shared_http import _settings

    versions = {"fastmcp": "4.1.4", "mcp": "2.3.0", "uvicorn": "0.54.0"}
    monkeypatch.setattr(_settings, "version", versions.__getitem__)
    require_qualified_stack()
    versions["fastmcp"] = "4.1.5"
    with pytest.raises(RuntimeError):
        require_qualified_stack()

    def missing(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(_settings, "version", missing)
    with pytest.raises(RuntimeError):
        require_qualified_stack()


@pytest.mark.parametrize("value", [0, -3, 3, "", "fixture-id", "123x", "_GET_streamx", "9" * 5000],
                         ids=["zero", "negative", "integer", "empty", "text", "mixed", "different-sentinel", "long-digits"])
def test_admitted_ids_retain_the_exact_wire_type(value):
    body = json.dumps({"jsonrpc": "2.0", "id": value, "method": "ping"}).encode()
    parsed = validate_envelope(body)
    assert parsed["id"] == value and type(parsed["id"]) is type(value)


@pytest.mark.parametrize("value", ["0", "-1", "+1", "01", " 1 ", "1_0", "١", "_GET_stream", True, 1.2])
def test_unsafe_root_id_refuses_before_dispatch(value):
    with pytest.raises(ValueError):
        validate_envelope(json.dumps({"jsonrpc": "2.0", "id": value, "method": "ping"}).encode())


def test_nullable_error_id_and_ignored_cancel_alias_keep_sdk_semantics():
    validate_envelope(b'{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"fixture"}}')
    validate_envelope(b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"request_id":"1"}}')
    validate_envelope(b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":true}}')
    with pytest.raises(ValueError):
        validate_envelope(b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":"1"}}')


@pytest.mark.parametrize("body", [
    b'[]', b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]', b'null', b'{', b'{}',
    b'{"jsonrpc":"2.0","id":1,"id":1,"method":"ping"}',
    b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":1,"requestId":2}}',
    b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"_meta":{"x":1,"x":2}}}',
    b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"value":NaN}}',
    b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"value":Infinity}}',
])
def test_malformed_structural_envelopes_refuse(body):
    with pytest.raises(ValueError):
        validate_envelope(body)


async def test_bounded_body_replay_preserves_original_bytes_and_argument_semantics():
    from mcp.server.transport_security import RequestBodyLimitMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    body = b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"fixture","arguments":{"x":1,"x":2}}}'
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}
    incoming = iter([{"type": "http.request", "body": body[:20], "more_body": True},
                     {"type": "http.request", "body": body[20:], "more_body": False}])
    seen, messages = [], []

    async def receive():
        return next(incoming)

    async def send(message):
        messages.append(message)

    async def endpoint(scope, receive, send):
        value = await Request(scope, receive).body()
        seen.append(value)
        await Response("executed")(scope, receive, send)

    await RequestBodyLimitMiddleware(EnvelopeGuard(endpoint), max_body_size=len(body))(scope, receive, send)
    assert seen == [body]
    assert validate_envelope(body)["params"]["arguments"]["x"] == 2
    assert messages[0]["status"] == 200


@pytest.mark.parametrize("declared", [False, True])
async def test_known_or_chunked_oversize_never_dispatches(declared):
    from mcp.server.transport_security import RequestBodyLimitMiddleware

    called = []
    messages = []
    body = b'{"jsonrpc":"2.0","id":3,"method":"ping"}'
    scope = {"type": "http", "method": "POST", "path": "/mcp",
             "headers": [(b"content-length", str(len(body)).encode())] if declared else []}
    incoming = iter([{"type": "http.request", "body": body[:5], "more_body": True},
                     {"type": "http.request", "body": body[5:], "more_body": False}])

    async def receive():
        return next(incoming)

    async def send(message):
        messages.append(message)

    async def endpoint(scope, receive, send):
        called.append(True)

    await RequestBodyLimitMiddleware(EnvelopeGuard(endpoint), max_body_size=len(body)-1)(scope, receive, send)
    assert called == []
    assert messages[0]["status"] == 413
