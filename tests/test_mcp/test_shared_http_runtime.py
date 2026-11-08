"""Actual SDK dispatch and caller/resource boundaries with synthetic tools."""

import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace

import aiosqlite
import httpx
import pytest
from fastmcp import FastMCP

import genesis.caller_context as identity
from genesis.caller_credentials import CallerResolver
from genesis.db.request_database import RequestDatabase
from genesis.mcp.external_profiles import current_external_profile
from genesis.mcp.shared_http import SharedHTTPConfig, build_http_app
from genesis.observability.mcp_middleware import install_instrumentation
from genesis.observability.provider_activity import ProviderActivityTracker


def unpack(response):
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        messages = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        return next(item for item in messages if "result" in item or "error" in item)
    return response.json()


@pytest.fixture
async def runtime(tmp_path, monkeypatch, request):
    for name in identity.IDENTITY_ENV_VARS:
        monkeypatch.setenv(name, "fixture")
    monkeypatch.setattr(identity, "_shared_service", False)
    path = tmp_path / "shared.db"
    async with aiosqlite.connect(path) as connection:
        await connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE update_history(status TEXT, completed_at TEXT, new_commit TEXT);
            CREATE TABLE activity_log(provider TEXT, latency_ms REAL, success INTEGER,
                                      cache_hit INTEGER, error_message TEXT, created_at TEXT);
        """)
    db = RequestDatabase(path, max_units=2, max_background_units=1, max_background_jobs=4)
    tracker = ProviderActivityTracker()
    tracker.set_db(db)
    server = FastMCP("shared-fixture")
    state = SimpleNamespace(enabled=True, calls=0, entered=asyncio.Event(), release=asyncio.Event())

    async def health_status(marker: str = "fixture", wait: bool = False, fail: bool = False):
        state.calls += 1
        tracker.record(marker, latency_ms=1, success=True)
        state.entered.set()
        if wait:
            await state.release.wait()
        if fail:
            raise ValueError("synthetic failure")
        # Exercise a real unit, not just context propagation in isolation.
        await db.execute("SELECT 1")
        return {**asdict(identity.caller_context()), "role": current_external_profile.get()}

    health_status.__module__ = "genesis.mcp.health.status"
    server.tool(health_status)

    async def hidden():
        state.calls += 1
        return "unavailable"

    hidden.__module__ = "genesis.mcp.health.status"
    server.tool(hidden, name="private_fixture")

    async def local():
        state.calls += 1
        return "local"

    local.__module__ = "genesis.mcp.health.browser"
    server.tool(local, name="local_fixture")
    install_instrumentation(server, tracker, "health", db)
    config = SharedHTTPConfig("health", "http://127.0.0.1:9123/mcp", 2, 1, 2,
                              getattr(request, "param", 30), 4096)
    resolver = CallerResolver("f" * 43, {"external": "e" * 43, "validator": "v" * 43})
    app = await build_http_app(server, config=config, resolver=resolver, database=db, tracker=tracker,
                               loaded_commit="a" * 40, started_at="2026-01-01T00:00:00+00:00",
                               enabled=lambda: state.enabled)
    headers = {"Authorization": "Bearer " + "e" * 43, "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2026-07-28"}
    ready, stop = asyncio.get_running_loop().create_future(), asyncio.Event()

    async def own_lifespan():
        try:
            async with app.router.lifespan_context(app):
                ready.set_result(None)
                await stop.wait()
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
            raise

    owner = asyncio.create_task(own_lifespan())
    try:
        await ready
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url="http://127.0.0.1:9123", headers=headers) as client:
            yield SimpleNamespace(app=app, client=client, db=db, path=path, tracker=tracker, state=state)
    finally:
        state.release.set()
        stop.set()
        await owner
        if tracker._flush_task is not None:
            await tracker._flush_task
        await db.close()


async def rpc(runtime, method, params=None, **options):
    params = {**(params or {}), "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                        "io.modelcontextprotocol/clientCapabilities": {}}}
    headers = {"mcp-method": method, **options.pop("headers", {})}
    if method == "tools/call":
        headers["mcp-name"] = params["name"]
    return await runtime.client.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": method,
                                                 "params": params}, headers=headers, **options)


async def call(runtime, name="health_status", arguments=None, **options):
    return await rpc(runtime, "tools/call", {"name": name, "arguments": arguments or {}}, **options)


async def persisted(runtime):
    if runtime.tracker._flush_task is not None:
        await runtime.tracker._flush_task
    # A record appended while an existing flush runs may remain queued until
    # another record schedules a flush. Observe after explicit batch settlement;
    # this test does not assert immediate/automatic durability.
    if runtime.tracker._write_batch:
        async with runtime.db.unit_of_work():
            await runtime.tracker._flush_to_db()
    async with aiosqlite.connect(runtime.path) as observer:
        cursor = await observer.execute("SELECT provider FROM activity_log")
        return [row[0] for row in await cursor.fetchall()]


async def test_real_message_suppression_and_ordinary_followup(runtime):
    validator = await call(runtime, arguments={"marker": "validator"},
                           headers={"Authorization": "Bearer " + "v" * 43})
    assert validator.status_code == 200
    result = unpack(validator)["result"]
    assert not result.get("isError")
    assert result["structuredContent"]["profile"] == "validator"
    assert await persisted(runtime) == []
    assert sum(row["calls"] for row in runtime.tracker.summary()) == 2
    ordinary = await call(runtime, arguments={"marker": "ordinary"})
    assert not unpack(ordinary)["result"].get("isError")
    assert sorted(await persisted(runtime)) == ["mcp.health.health_status", "ordinary"]
    assert runtime.tracker.persist_activity is True
    assert current_external_profile.get() is None


async def test_auth_and_authority_apply_to_status_and_controls(runtime):
    for method, path in [("GET", "/status"), ("HEAD", "/status"), ("GET", "/mcp"), ("DELETE", "/mcp")]:
        invalid = await runtime.client.request(method, path, headers={"Authorization": "Bearer invalid"})
        assert invalid.status_code == 401
        wrong_host = await runtime.client.request(method, path, headers={"Host": "127.0.0.1:9124"})
        assert wrong_host.status_code == 421
    status = await runtime.client.get("/status")
    assert status.status_code == 200 and status.json()["stale"] is None
    assert "external_untrusted" not in status.text and "genesis_caller" not in status.text
    assert (await runtime.client.head("/status")).content == b""


async def test_profile_and_local_refusal_precede_body_and_unit(runtime):
    listed = unpack(await rpc(runtime, "tools/list"))
    assert [tool["name"] for tool in listed["result"]["tools"]] == ["health_status"]
    for name in ("private_fixture", "local_fixture", "unknown"):
        reply = unpack(await call(runtime, name))
        assert reply.get("error") or reply["result"].get("isError")
    assert runtime.state.calls == 0 and await persisted(runtime) == []
    runtime.state.enabled = False
    assert (await runtime.client.get("/status")).json()["enabled"] is False
    reply = unpack(await call(runtime))
    assert reply.get("error") or reply["result"].get("isError")
    assert runtime.state.calls == 0


async def test_busy_refusal_keeps_first_work_and_status_available(runtime):
    first = asyncio.create_task(call(runtime, arguments={"wait": True}))
    try:
        await asyncio.wait_for(runtime.state.entered.wait(), 2)
        second = unpack(await call(runtime))
        assert second["error"]["message"] == "Shared MCP service busy"
        status = (await runtime.client.get("/status")).json()
        assert status["active_tools"] == 1 and status["busy_refusals"] == 1
        assert runtime.state.calls == 1
    finally:
        runtime.state.release.set()
        reply = await first
    assert not unpack(reply)["result"].get("isError")
    assert (await runtime.client.get("/status")).json()["active_tools"] == 0


async def test_freshness_recomputes_after_rollback(runtime):
    async with runtime.db.unit_of_work():
        await runtime.db.execute("INSERT INTO update_history VALUES ('success', ?, ?)",
                                 ("2026-02-01T00:00:00+00:00", "b" * 40))
    assert (await runtime.client.get("/status")).json()["stale"] is True
    async with runtime.db.unit_of_work():
        await runtime.db.execute("UPDATE update_history SET new_commit=?", ("a" * 40,))
    assert (await runtime.client.get("/status")).json()["stale"] is False


async def test_task_augmentation_refused_before_tool_body(runtime):
    response = await rpc(runtime, "tools/call", {"name": "health_status", "task": {"ttl": 1000}})
    if response.status_code == 200:
        reply = unpack(response)
        assert reply.get("error") or reply["result"].get("isError")
    else:
        assert response.status_code == 400
    assert runtime.state.calls == 0


@pytest.mark.parametrize("token", ["e", "v"])
async def test_tool_error_releases_admission_and_keeps_profile_policy(runtime, token):
    response = await call(runtime, arguments={"marker": "error-marker", "fail": True},
                          headers={"Authorization": "Bearer " + token * 43})
    assert unpack(response)["result"]["isError"] is True
    assert (await runtime.client.get("/status")).json()["active_tools"] == 0
    rows = await persisted(runtime)
    assert ("error-marker" in rows) is (token == "e")
    assert ("mcp.health.health_status" in rows) is (token == "e")
    assert not unpack(await call(runtime))["result"].get("isError")


async def test_invalid_auth_precedes_malformed_and_oversized_body(runtime):
    for body in (b"invalid-json", b"x" * 5000):
        response = await runtime.client.post("/mcp", content=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer invalid"})
        assert response.status_code == 401
    valid = await runtime.client.post("/mcp", content=b"x" * 5000,
                                     headers={"Content-Type": "application/json"})
    assert valid.status_code == 413 and runtime.state.calls == 0


async def initialize(runtime):
    return await runtime.client.post("/mcp", headers={"MCP-Protocol-Version": "2025-11-25"},
        json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "fixture", "version": "1"}}})


async def test_session_cap_and_authenticated_delete(runtime):
    first, second = await initialize(runtime), await initialize(runtime)
    assert first.status_code == second.status_code == 200
    assert (await initialize(runtime)).status_code == 503
    headers = {"MCP-Protocol-Version": "2025-11-25", "Mcp-Session-Id": first.headers["mcp-session-id"]}
    refused = await runtime.client.delete("/mcp", headers={**headers, "Authorization": "Bearer " + "v" * 43})
    assert refused.status_code == 404
    assert (await runtime.client.delete("/mcp", headers=headers)).status_code == 200
    assert (await initialize(runtime)).status_code == 200


@pytest.mark.parametrize("runtime", [.1], indirect=True)
async def test_configured_idle_timeout_releases_session(runtime):
    first = await initialize(runtime)
    assert first.status_code == 200
    await asyncio.sleep(.25)
    expired = await runtime.client.delete("/mcp", headers={"MCP-Protocol-Version": "2025-11-25",
                                                         "Mcp-Session-Id": first.headers["mcp-session-id"]})
    assert expired.status_code == 404
    assert (await initialize(runtime)).status_code == 200
