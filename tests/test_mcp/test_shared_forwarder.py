"""Actual forwarder CLI, raw native frames and the four owned service factories."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import asyncio
import json
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from tests.test_mcp.test_shared_cli_startup_contract import result
from tests.test_mcp.test_shared_cli_startup_contract import shared_install as shared_install

ROOT = Path(__file__).resolve().parents[2]
MODERN = "2026-07-28"
LEGACY = "2025-06-18"


def frame(request_id, method, **params):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {
        **params, "_meta": {"io.modelcontextprotocol/protocolVersion": MODERN,
                           "io.modelcontextprotocol/clientCapabilities": {}},
    }}


async def send(child, message):
    child.stdin.write(json.dumps(message).encode() + b"\n")
    await child.stdin.drain()


async def receive(child):
    raw = await asyncio.wait_for(child.stdout.readline(), 30)
    assert raw, ("frontend exited", child.returncode)
    return json.loads(raw)


def seeded(payload, service, observation):
    if service == "health":
        return payload["queues"] == {"fixture": 7}
    if service in {"memory", "recon"}:
        return payload == {"result": [observation]}
    return payload == {
        "items": [{"id": "cli-pending", "category": "fixture", "channel": "telegram",
                   "urgency": "low", "deliver_after": None, "created_at": "2026-01-01",
                   "message_preview": "Fixture message"}],
        "total": 1, "offset": 0, "limit": 50, "truncated": False,
    }


@pytest.mark.parametrize("service,tool", [
    ("health", "health_status"), ("memory", "observation_query"),
    ("outreach", "outreach_pending"), ("recon", "recon_findings"),
])
async def test_actual_cli_preserves_full_catalog_seed_ids_and_eof(tmp_path, service, tool, shared_install):
    env = dict(shared_install.env, GENESIS_CC_SESSION="1", GENESIS_SESSION_ORIGIN="external_untrusted")
    port = shared_install.port
    backend = frontend = None
    headers = {"Authorization": "Bearer " + "f" * 43,
               "X-Genesis-Caller": json.dumps({"dispatched": True, "supervised": False,
                                              "origin": "external_untrusted"}),
               "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": MODERN}
    with (tmp_path / "backend.stderr").open("wb") as backend_error, \
            (tmp_path / "frontend.stderr").open("wb") as frontend_error:
        try:
            backend = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name("shared_cli_lifecycle_child.py")),
                service, str(port), str(shared_install.events), cwd="/", env=env,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=backend_error,
            )
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                         headers=headers, trust_env=False) as control:
                async with asyncio.timeout(180):
                    while True:
                        assert backend.returncode is None
                        try:
                            if (await control.get("/status")).status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        await asyncio.sleep(.02)
                catalog_request = frame(3, "tools/list")
                tool_request = frame("fixture-call", "tools/call", name=tool, arguments={})
                catalog = result(await control.post("/mcp", json=catalog_request,
                                                    headers={"mcp-method": "tools/list"}))
                oracle = result(await control.post("/mcp", json=tool_request,
                                                   headers={"mcp-method": "tools/call", "mcp-name": tool}))
                assert seeded(oracle["structuredContent"], service, shared_install.observation)
                frontend = await asyncio.create_subprocess_exec(
                    sys.executable, str(Path(__file__).with_name("shared_forwarder_child.py")),
                    service, str(port), cwd="/", env=env, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=frontend_error,
                    limit=len(json.dumps(catalog).encode()) + 4096,
                )
                await send(frontend, {**catalog_request, "id": True})
                refused = await receive(frontend)
                assert refused["id"] is None and refused["error"]["code"] == -32600
                await send(frontend, catalog_request)
                listed = await receive(frontend)
                assert type(listed["id"]) is int and listed["id"] == 3
                assert listed["result"] == catalog
                await send(frontend, tool_request)
                called = await receive(frontend)
                assert called["id"] == "fixture-call" and not called["result"].get("isError")
                assert seeded(called["result"]["structuredContent"], service, shared_install.observation)
                frontend.stdin.close()
                assert await asyncio.wait_for(frontend.wait(), 30) == 0
        finally:
            if frontend is not None and frontend.returncode is None:
                frontend.terminate()
                await frontend.wait()
            if backend is not None and backend.returncode is None:
                backend.send_signal(signal.SIGTERM)
                await asyncio.wait_for(backend.wait(), 60)
    diagnostics = (tmp_path / "frontend.stderr").read_text()
    assert "FIXTURE_STRAY_STDOUT" in diagnostics
    assert "FIXTURE_SENSITIVE_SDK_DIAGNOSTIC" not in diagnostics
    events = [json.loads(line) for line in shared_install.events.read_text().splitlines()]
    assert events.count("resources-closed") == 1
    assert events.index("lifespan-stopped") < events.index("resources-closed")
    assert events.index("drain-confirmed") < events.index("resources-closed")


def effects(directory):
    path = directory / "effects.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def observed(directory, event, **values):
    async with asyncio.timeout(30):
        while True:
            rows = [row for row in effects(directory)
                    if row["event"] == event and all(row.get(k) == v for k, v in values.items())]
            if rows:
                return rows[-1]
            await asyncio.sleep(.01)


@asynccontextmanager
async def synthetic(tmp_path, install):
    env = dict(install.env, GENESIS_CC_SESSION="1", GENESIS_SESSION_ORIGIN="external_untrusted")
    children, streams = [], []
    headers = {"Authorization": "Bearer " + "f" * 43,
               "X-Genesis-Caller": json.dumps({"dispatched": True, "supervised": False,
                                              "origin": "external_untrusted"}),
               "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": MODERN}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{install.port}", headers=headers,
                                 trust_env=False, timeout=30) as control:
        async def backend():
            stream = (tmp_path / f"backend-{len(children)}.stderr").open("wb")
            streams.append(stream)
            child = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name("shared_forwarder_backend.py")),
                str(tmp_path), str(install.port), env=env, cwd="/", stderr=stream,
                stdout=asyncio.subprocess.DEVNULL)
            children.append(child)
            async with asyncio.timeout(60):
                while True:
                    assert child.returncode is None
                    try:
                        if (await control.get("/status")).status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(.02)
            oracle = result(await control.post("/mcp", json=frame("oracle", "tools/call",
                name="health_status", arguments={}), headers={"mcp-method": "tools/call",
                                                              "mcp-name": "health_status"}))
            assert oracle["structuredContent"] == {"marker": "seed", "seed": 7, "payload": ""}
            return child

        async def frontend(extra=None, limit=2_500_000):
            stream = (tmp_path / f"frontend-{len(children)}.stderr").open("wb")
            streams.append(stream)
            child = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name("shared_forwarder_child.py")),
                "health", str(install.port), env={**env, **(extra or {})}, cwd="/", stderr=stream,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=limit)
            children.append(child)
            return child

        try:
            yield backend, frontend
        finally:
            # Release instrument gates before joining the owned children.
            for name in ("init", "delete"):
                (tmp_path / ("release-" + name)).touch()
            for child in reversed(children):
                if child.returncode is None:
                    child.terminate()
                    try:
                        await asyncio.wait_for(child.wait(), 30)
                    except TimeoutError:
                        child.kill()
                        # An intentionally unread/full asyncio pipe must be drained
                        # before its transport's wait completes, even after OS exit.
                        if child.stdout:
                            await child.stdout.read()
                        await child.wait()
            for stream in streams:
                stream.close()


async def initialize(child, era):
    if era == LEGACY:
        await send(child, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": era, "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}})
        assert (await receive(child))["result"]["protocolVersion"] == era
        await send(child, {"jsonrpc": "2.0", "method": "notifications/initialized"})


def call(request_id, marker, era, **arguments):
    message = frame(request_id, "tools/call", name="health_status", arguments={"marker": marker, **arguments})
    if era == LEGACY:
        message["params"].pop("_meta")
    return message


@pytest.mark.parametrize("service", ["health"])
@pytest.mark.parametrize("era", [MODERN, LEGACY])
@pytest.mark.parametrize("ending", ["cancel", "EOF", "crash", "graceful"])
async def test_real_pipe_cancellation_and_unknown_outcome_never_replay(
    tmp_path, service, era, ending, shared_install,
):
    async with synthetic(tmp_path, shared_install) as (start_backend, start_frontend):
        backend, frontend = await start_backend(), await start_frontend()
        await initialize(frontend, era)
        await send(frontend, call(0, "effect", era, wait=True))
        await observed(tmp_path, "entered", marker="effect")
        entered = await observed(tmp_path, "post", method="tools/call", request_id=0)
        assert type(entered["request_id"]) is int
        if ending in {"cancel", "EOF"}:
            if ending == "cancel":
                await send(frontend, {"jsonrpc": "2.0", "method": "notifications/cancelled",
                                      "params": {"requestId": 0}})
                await send(frontend, call("barrier", "barrier", era))
                assert (await receive(frontend))["result"]["structuredContent"]["seed"] == 7
            else:
                frontend.stdin.close()
                assert await asyncio.wait_for(frontend.wait(), 10) == 0
            await observed(tmp_path, "cancelled", marker="effect")
        else:
            backend.send_signal(signal.SIGKILL if ending == "crash" else signal.SIGTERM)
            await asyncio.wait_for(backend.wait(), 30)
            failed = await receive(frontend)
            assert failed["id"] == 0 and type(failed["id"]) is int
            assert failed["error"]["message"] == "Outcome unknown; not resent"
            await start_backend()
            await send(frontend, call("later", "later", era))
            assert (await receive(frontend))["result"]["structuredContent"]["marker"] == "later"
        assert sum(row["event"] == "entered" and row.get("marker") == "effect"
                   for row in effects(tmp_path)) == 1
        if frontend.returncode is None:
            frontend.stdin.close()
            assert await asyncio.wait_for(frontend.wait(), 10) == 0


@pytest.mark.parametrize("service", ["health"])
@pytest.mark.parametrize("era", [MODERN, LEGACY])
async def test_long_large_reply_and_complete_metadata_preserve_typed_id(
    tmp_path, service, era, shared_install,
):
    async with synthetic(tmp_path, shared_install) as (start_backend, start_frontend):
        await start_backend()
        frontend = await start_frontend()
        await initialize(frontend, era)
        request = call("long", "long", era, wait=True, payload_bytes=1_200_000)
        custom = {"empty": {}, "nullable": None, "nested": [1, "unicode-λ"]}
        request["params"].setdefault("_meta", {}).update({"fixture.custom": custom,
                                                      "progressToken": "original-token"})
        await send(frontend, request)
        entered = await observed(tmp_path, "entered", marker="long")
        assert entered["request_id"] == "long"
        assert entered["metadata"]["fixture.custom"] == custom
        await asyncio.sleep(6.5)  # Exceeds the measured accidental HTTPX five-second deadline.
        (tmp_path / "release-long").touch()
        progress = await receive(frontend)
        assert progress["method"] == "notifications/progress"
        assert progress["params"]["progressToken"] == "original-token"
        reply = await receive(frontend)
        assert reply["id"] == "long"
        assert reply["result"]["structuredContent"] == {"marker": "long", "seed": 7,
                                                       "payload": "x" * 1_200_000}
        frontend.stdin.close()
        assert await asyncio.wait_for(frontend.wait(), 10) == 0


@pytest.mark.parametrize("service", ["health"])
@pytest.mark.parametrize("queued", [0, 4])
async def test_held_initialization_and_queued_calls_do_not_hide_eof(
    tmp_path, service, queued, shared_install,
):
    (tmp_path / "hold-init").touch()
    async with synthetic(tmp_path, shared_install) as (start_backend, start_frontend):
        await start_backend()
        frontend = await start_frontend()
        await send(frontend, {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {
            "protocolVersion": LEGACY, "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}})
        await observed(tmp_path, "initialize-entered")
        for number in range(queued):
            await send(frontend, call(number, "queued", LEGACY))
        frontend.stdin.close()
        assert await asyncio.wait_for(frontend.wait(), 10) == 0
        assert not any(row["event"] == "entered" and row.get("marker") == "queued"
                       for row in effects(tmp_path))


@pytest.mark.parametrize("service", ["health"])
@pytest.mark.parametrize("era", [MODERN, LEGACY])
@pytest.mark.parametrize("invalid", [None,
    b'{"jsonrpc":"2.0","id":true,"method":"tools/list"}',
    b'{"jsonrpc":"2.0","id":1,"id":2,"method":"tools/list"}',
    b'\xef\xbb\xbf{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    b'{"jsonrpc":"2.0","id":"\xff","method":"tools/list"}',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"task":{}}}',
], ids=["valid", "bool", "duplicate", "BOM", "UTF8", "task"])
async def test_full_output_pipe_does_not_block_eof(tmp_path, service, era, invalid, shared_install):
    async with synthetic(tmp_path, shared_install) as (start_backend, start_frontend):
        await start_backend()
        frontend = await start_frontend(limit=1024)
        await initialize(frontend, era)
        await send(frontend, call("large", "large", era, payload_bytes=1_200_000))
        await observed(tmp_path, "entered", marker="large")
        async with asyncio.timeout(10):
            while not frontend.stdout._paused:
                await asyncio.sleep(.01)
        assert len(frontend.stdout._buffer) > 1024  # Prove backpressure before ending input.
        if invalid is not None:
            frontend.stdin.write(invalid + b"\n")
            await frontend.stdin.drain()
        frontend.stdin.close()
        async with asyncio.timeout(10):
            while frontend.returncode is None:
                await asyncio.sleep(.01)
        assert frontend.returncode == 0
        await frontend.stdout.read()  # Reap asyncio's pipe transport after observing OS exit.
        await frontend.wait()


@pytest.mark.parametrize("service", ["health"])
@pytest.mark.parametrize("grace", [0, .25])
async def test_hung_delete_obeys_configured_grace(tmp_path, service, grace, shared_install):
    path = Path(shared_install.env["GENESIS_HOME"]) / "config/mcp_shared.json"
    spec = json.loads(path.read_text())
    spec["servers"][service]["shutdown_grace_s"] = grace
    path.write_text(json.dumps(spec))
    (tmp_path / "hold-delete").touch()
    async with synthetic(tmp_path, shared_install) as (start_backend, start_frontend):
        await start_backend()
        frontend = await start_frontend()
        await initialize(frontend, LEGACY)
        await send(frontend, call(1, "seed", LEGACY))
        assert (await receive(frontend))["result"]["structuredContent"]["seed"] == 7
        frontend.stdin.close()
        assert await asyncio.wait_for(frontend.wait(), 10) == 0
        if grace:
            await observed(tmp_path, "delete-entered")


@pytest.mark.parametrize("service", ["health"])
@pytest.mark.parametrize("era,method", [(MODERN, "tools/call"), (LEGACY, "tools/call"),
    (MODERN, "tools/list"), (LEGACY, "tools/list"), (LEGACY, "initialize")])
async def test_malformed_reply_has_safe_original_id_and_is_never_resent(
    tmp_path, service, era, method, shared_install,
):
    async with synthetic(tmp_path, shared_install) as (start_backend, start_frontend):
        await start_backend()
        frontend = await start_frontend()
        if method != "initialize":
            await initialize(frontend, era)
        request = call("original", "corrupt", era) if method == "tools/call" else frame("original", method)
        if era == LEGACY:
            request["params"].pop("_meta", None)
        if method == "initialize":
            request["params"] = {"protocolVersion": era, "capabilities": {},
                                 "clientInfo": {"name": "fixture", "version": "1"}}
        request["params"].setdefault("_meta", {})["fixture.corrupt"] = True
        await send(frontend, request)
        reply = await receive(frontend)
        await observed(tmp_path, "corrupt-response", kind="JSON" if era == MODERN else "SSE")
        expected = "Outcome unknown; not resent" if method == "tools/call" else "Invalid shared MCP response"
        assert reply["id"] == "original" and reply["error"] == {
            "code": -32700, "message": expected, "data": {"transport_error_code": -32700}}
        if method == "tools/call":
            await send(frontend, call("later", "later", era))
            assert (await receive(frontend))["result"]["structuredContent"]["marker"] == "later"
            assert sum(row["event"] == "entered" and row.get("marker") == "corrupt"
                       for row in effects(tmp_path)) == 1
        frontend.stdin.close()
        assert await asyncio.wait_for(frontend.wait(), 10) == 0
    for path in tmp_path.glob("frontend-*.stderr"):
        assert "HARMLESS_PRIVATE_SENTINEL" not in path.read_text()


@pytest.mark.parametrize("service", ["health"])
async def test_modern_initialize_refusal_does_not_release_queued_tools(tmp_path, service, shared_install):
    async with synthetic(tmp_path, shared_install) as (start_backend, start_frontend):
        await start_backend()
        frontend = await start_frontend()
        await send(frontend, frame("unsupported-init", "initialize", protocolVersion=MODERN,
                                   capabilities={}, clientInfo={"name": "fixture", "version": "1"}))
        await send(frontend, call("queued", "must-not-execute", MODERN))
        replies = {row["id"]: row for row in [await receive(frontend), await receive(frontend)]}
        (tmp_path / "refusal-replies.json").write_text(json.dumps(replies))
        assert replies["unsupported-init"]["error"] == {
            "code": -32601, "message": "Method not found", "data": "initialize"}
        assert replies["queued"]["error"] == {"code": -32600, "message": "Initialization did not complete"}
        assert not any(row.get("marker") == "must-not-execute" for row in effects(tmp_path))
        frontend.stdin.close()
        assert await asyncio.wait_for(frontend.wait(), 10) == 0


@pytest.mark.parametrize("method", ["tools/call", "tools/list", "initialize"])
async def test_business_parse_error_is_not_mistaken_for_transport(method):
    from types import SimpleNamespace

    from mcp.shared.exceptions import MCPError

    from genesis.mcp.shared_client._connection import RemotePeer

    class Dispatcher:
        async def send_raw_request(self, *args, **kwargs):
            raise MCPError(-32700, "fixture business error", data={"fixture": 7})

    settings = SimpleNamespace(http=SimpleNamespace(service="health", url="http://127.0.0.1:9123/mcp"))
    peer = RemotePeer(None, settings, None)

    async def generation():
        return SimpleNamespace(dispatcher=Dispatcher(), session_missing=False)

    peer._get = generation
    with pytest.raises(MCPError) as caught:
        await peer.request(SimpleNamespace(request_id="original"), method, {})
    assert caught.value.code == -32700 and caught.value.message == "fixture business error"
    assert caught.value.data == {"fixture": 7}


@pytest.mark.parametrize("service", ["health"])
@pytest.mark.parametrize("era", [MODERN, LEGACY])
@pytest.mark.parametrize("kind", ["redirect-307", "redirect-308", "accepted", "content-type"])
async def test_http_fault_after_durable_effect_is_not_resent(tmp_path, service, era, kind, shared_install):
    async with synthetic(tmp_path, shared_install) as (start_backend, start_frontend):
        await start_backend()
        frontend = await start_frontend()
        await initialize(frontend, era)
        request = call("original", "http-fault", era)
        request["params"].setdefault("_meta", {})["fixture.response"] = kind
        await send(frontend, request)
        reply = await receive(frontend)
        (tmp_path / "fault-reply.json").write_text(json.dumps(reply))
        await observed(tmp_path, "fault-response", kind=kind)
        assert sum(row["event"] == "entered" and row.get("marker") == "http-fault"
                   for row in effects(tmp_path)) == 1
        assert reply["id"] == "original"
        assert reply["error"]["code"] == -32600
        assert reply["error"]["message"] == "Outcome unknown; not resent"
        await send(frontend, call("later", "later", era))
        assert (await receive(frontend))["result"]["structuredContent"]["marker"] == "later"
        frontend.stdin.close()
        assert await asyncio.wait_for(frontend.wait(), 10) == 0
    # Include late effects before the backend owner settles; a count immediately
    # after the native reply can miss a replay that is still finishing.
    assert sum(row["event"] == "entered" and row.get("marker") == "http-fault"
               for row in effects(tmp_path)) == 1


@pytest.mark.parametrize("service", ["health"])
@pytest.mark.parametrize("era", [MODERN, LEGACY])
async def test_busy_is_preserved_without_retiring_pending_neighbors(tmp_path, service, era, shared_install):
    async with synthetic(tmp_path, shared_install) as (start_backend, start_frontend):
        await start_backend()
        frontend = await start_frontend()
        await initialize(frontend, era)
        for number in range(2):
            await send(frontend, call(number, f"wait-{number}", era, wait=True))
            await observed(tmp_path, "entered", marker=f"wait-{number}")
        await send(frontend, call("busy", "busy", era))
        refused = await receive(frontend)
        assert refused == {"jsonrpc": "2.0", "id": "busy", "error": {
            "code": -32000, "message": "Shared MCP service busy"}}
        for number in range(2):
            (tmp_path / f"release-wait-{number}").touch()
        completed = [await receive(frontend), await receive(frontend)]
        assert {row["id"] for row in completed} == {0, 1}
        assert all(row["result"]["structuredContent"]["seed"] == 7 for row in completed)
        frontend.stdin.close()
        assert await asyncio.wait_for(frontend.wait(), 10) == 0


@pytest.mark.parametrize("service", ["health"])
async def test_two_cli_identities_are_distinct_and_bad_credentials_never_execute(
    tmp_path, service, shared_install,
):
    async with synthetic(tmp_path, shared_install) as (start_backend, start_frontend):
        await start_backend()
        for index in range(2):
            frontend = await start_frontend({"GENESIS_SESSION_ID": f"fixture-{index}"})
            await send(frontend, call(1, f"identity-{index}", MODERN))
            assert (await receive(frontend))["result"]["structuredContent"]["seed"] == 7
            row = await observed(tmp_path, "entered", marker=f"identity-{index}")
            assert row["session_id"] == f"fixture-{index}"
            frontend.stdin.close()
            assert await asyncio.wait_for(frontend.wait(), 10) == 0
        token = Path(shared_install.env["GENESIS_HOME"]) / "mcp_credentials/forwarder"
        token.write_text("g" * 43 + "\n")
        frontend = await start_frontend()
        await send(frontend, call("refused", "must-not-execute", MODERN))
        assert "error" in await receive(frontend)
        assert not any(row.get("marker") == "must-not-execute" for row in effects(tmp_path))
        frontend.stdin.close()
        assert await asyncio.wait_for(frontend.wait(), 10) == 0


@pytest.mark.parametrize("service", ["health"])
async def test_raw_invalid_frames_are_refused_before_typed_normalization(tmp_path, service, shared_install):
    invalid = [b'{"jsonrpc":"2.0","id":true,"method":"tools/list"}',
               b'{"jsonrpc":"2.0","id":1,"id":2,"method":"tools/list"}',
               b'{"jsonrpc":"2.0","id":"1","method":"tools/list"}',
               b'{"jsonrpc":"2.0","id":"_GET_stream","method":"tools/list"}',
               b'\xef\xbb\xbf{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
               b'{"jsonrpc":"2.0","id":"\xff","method":"tools/list"}']
    async with synthetic(tmp_path, shared_install) as (start_backend, start_frontend):
        await start_backend()
        frontend = await start_frontend()
        for raw in invalid:
            frontend.stdin.write(raw + b"\n")
            await frontend.stdin.drain()
            assert await receive(frontend) == {"jsonrpc": "2.0", "id": None, "error": {
                "code": -32600, "message": "Invalid MCP envelope"}}
        assert not any(row["event"] == "post" and row.get("method") == "tools/list"
                       for row in effects(tmp_path))
        for request_id in [0, 2**53 - 1, "named-λ"]:
            await send(frontend, call(request_id, "valid", MODERN))
            response = await receive(frontend)
            assert response["id"] == request_id and type(response["id"]) is type(request_id)
            observed_id = (await observed(tmp_path, "post", method="tools/call",
                                          request_id=request_id))["request_id"]
            assert observed_id == request_id and type(observed_id) is type(request_id)
        frontend.stdin.close()
        assert await asyncio.wait_for(frontend.wait(), 10) == 0


@pytest.mark.parametrize("service", ["health"])
@pytest.mark.parametrize("route", ["GET", "POST"])
async def test_legacy_callback_preserves_opposite_direction_id(tmp_path, service, route, shared_install):
    async with synthetic(tmp_path, shared_install) as (start_backend, start_frontend):
        await start_backend()
        frontend = await start_frontend()
        await send(frontend, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": LEGACY, "capabilities": {"roots": {"listChanged": True}},
            "clientInfo": {"name": "fixture", "version": "1"}}})
        assert (await receive(frontend))["result"]["protocolVersion"] == LEGACY
        await send(frontend, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send(frontend, call(1, "callback-" + route, LEGACY))
        callback = await receive(frontend)
        row = await observed(tmp_path, "callback", route=route)
        assert callback["method"] == "roots/list" and callback["id"] == row["request_id"] == 1
        roots = [{"uri": "file:///fixture", "name": "fixture", "_meta": None}]
        await send(frontend, {"jsonrpc": "2.0", "id": callback["id"], "result": {"roots": roots}})
        reply = await receive(frontend)
        assert reply["id"] == 1 and reply["result"]["structuredContent"] == {"roots": roots}
        frontend.stdin.close()
        assert await asyncio.wait_for(frontend.wait(), 10) == 0
