"""Initial selection and immutable reserved metadata, before client input."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest

from genesis.mcp.database_target import TARGET_META, database_target, prepare_target_params
from genesis.mcp.shared_client import _target
from genesis.mcp.shared_paths import SharedPathError


def contract(target):
    return {"service": "memory", "enabled": True, "transport_schema_version": 1,
            "plan_bookmark_pending_version": 1, "catalog_revision": "a" * 64,
            "database_target_contract": {"version": 1, "target": target}}


@pytest.fixture
def target_client(tmp_path, monkeypatch):
    path = tmp_path / "selected.db"
    path.touch()
    settings = SimpleNamespace(http=SimpleNamespace(service="memory", origin="http://127.0.0.1:8091"))
    state = SimpleNamespace(path=path, settings=settings, entered=0, closed=0, requests=[], options=[],
                            status=contract(database_target(path)), code=200, body=None)
    actual = httpx2.AsyncClient

    async def respond(request):
        state.requests.append(request)
        # Response(json=None) means no body in this client, not literal JSON null.
        body = json.dumps(state.status).encode() if state.body is None else state.body
        return httpx2.Response(state.code, content=body, headers={"content-type": "application/json"}, request=request)

    class OwnedClient(actual):
        def __init__(self, **kwargs):
            state.options.append(kwargs)
            super().__init__(**kwargs, transport=httpx2.MockTransport(respond))

        async def __aenter__(self):
            state.entered += 1
            return await super().__aenter__()

        async def __aexit__(self, *exc):
            await super().__aexit__(*exc)
            state.closed += 1
            assert self.is_closed

    monkeypatch.setattr(_target.httpx2, "AsyncClient", OwnedClient)
    return state


async def test_matching_selected_target_uses_authenticated_status_and_closes(target_client):
    state = target_client
    headers = {"Authorization": "Bearer fixture", "X-Genesis-Caller": "fixture-caller"}
    result = await _target.select_memory_target(state.settings, lambda: headers, state.path)
    assert result == database_target(state.path)
    assert state.entered == state.closed == len(state.requests) == 1
    request = state.requests[0]
    assert request.method == "GET" and str(request.url) == state.settings.http.origin + "/status"
    assert all(request.headers[key] == value for key, value in headers.items())
    assert state.options[0] == {"headers": headers, "trust_env": False, "follow_redirects": False,
                               "max_redirects": 0, "timeout": _target.MCP_DEFAULT_TIMEOUT}


@pytest.mark.parametrize("kind", ["relative", "parent", "symlink", "missing", "directory"])
async def test_unprovable_paths_decline_before_headers_or_http(target_client, monkeypatch, tmp_path, kind):
    state = target_client
    path = state.path
    if kind == "relative":
        monkeypatch.chdir(tmp_path)
        path = Path(path.name)
    elif kind == "parent":
        (tmp_path / "child").mkdir()
        path = tmp_path / "child/../selected.db"
    elif kind == "symlink":
        path = tmp_path / "alias.db"
        path.symlink_to(state.path)
    elif kind == "missing":
        path = tmp_path / "uncreated.db"
    elif kind == "directory":
        path = tmp_path

    def forbidden_headers():
        pytest.fail("incomparable paths must not acquire credentials")

    assert await _target.select_memory_target(state.settings, forbidden_headers, path) is None
    assert not state.options and not state.requests and state.entered == state.closed == 0
    if kind == "missing":
        assert not path.exists()


@pytest.mark.parametrize("field,value", [
    ("service", "health"), ("enabled", False), ("enabled", 1),
    ("transport_schema_version", True), ("transport_schema_version", 2),
    ("catalog_revision", None), ("catalog_revision", True), ("catalog_revision", ""),
    ("catalog_revision", "a" * 63), ("catalog_revision", "g" * 64), ("catalog_revision", "A" * 64),
    ("plan_bookmark_pending_version", True), ("plan_bookmark_pending_version", 2),
    ("database_target_contract", None), ("database_target_contract", {}),
    ("database_target_contract", {"version": True, "target": "match"}),
    ("database_target_contract", {"version": 2, "target": "match"}),
    ("database_target_contract", {"version": 1, "target": "0" * 64}),
    ("database_target_contract", {"version": 1, "target": "match", "extra": False}),
])
async def test_status_refusal_closes_before_returning_to_launcher(target_client, field, value):
    state = target_client
    if isinstance(value, dict) and value.get("target") == "match":
        value = {**value, "target": database_target(state.path)}
    state.status[field] = value
    with pytest.raises(SharedPathError):
        await _target.select_memory_target(state.settings, lambda: {}, state.path)
    assert state.entered == state.closed == len(state.requests) == 1


@pytest.mark.parametrize("field", list(contract("fixture")))
async def test_each_missing_contract_field_refuses_and_closes(target_client, field):
    state = target_client
    state.status.pop(field)
    with pytest.raises(SharedPathError):
        await _target.select_memory_target(state.settings, lambda: {}, state.path)
    assert state.entered == state.closed == len(state.requests) == 1


@pytest.mark.parametrize("status", [None, [], "status", True, 1])
async def test_nonobject_status_refuses_and_closes(target_client, status):
    state = target_client
    state.status = status
    with pytest.raises(SharedPathError):
        await _target.select_memory_target(state.settings, lambda: {}, state.path)
    assert state.entered == state.closed == 1


@pytest.mark.parametrize("code,body", [(302, None), (401, None), (404, None), (500, None), (200, b"{bad"), (200, b"")])
async def test_http_and_json_failures_close_client(target_client, code, body):
    state = target_client
    state.code, state.body = code, body
    with pytest.raises((httpx2.HTTPStatusError, json.JSONDecodeError)):
        await _target.select_memory_target(state.settings, lambda: {}, state.path)
    assert state.entered == state.closed == len(state.requests) == 1


@pytest.mark.parametrize("metadata", [None, {}, {"progressToken": 0, "fixture": {"kept": True}}])
def test_target_merge_retains_input_and_other_metadata(metadata):
    params = {"name": "observation_query", "arguments": {"limit": 10}, "_meta": metadata}
    saved = copy.deepcopy(params)
    result = prepare_target_params("tools/call", params, "a" * 64)
    assert params == saved and result is not params
    assert result["arguments"] == params["arguments"]
    assert result["_meta"] == {**(metadata or {}), TARGET_META: {"version": 1, "target": "a" * 64}}


@pytest.mark.parametrize("metadata", [False, [], "meta", 1, {TARGET_META: None}, {TARGET_META: {"version": 1}}])
def test_client_reserved_collision_or_malformed_metadata_refuses(metadata):
    with pytest.raises(SharedPathError):
        prepare_target_params("tools/call", {"_meta": metadata}, "a" * 64)


@pytest.mark.parametrize("method,params", [("tools/list", {}), ("initialize", {}), ("tools/call", None)])
def test_target_does_not_change_other_messages(method, params):
    assert prepare_target_params(method, params, "a" * 64) is params
