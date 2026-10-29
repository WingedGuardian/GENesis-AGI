"""One fresh-process TCP probe, avoiding SSE-Starlette's cross-lifespan exit flag."""

import asyncio
import contextvars
import json
import runpy
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.middleware import RequireAuthMiddleware
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.http import StreamableHTTPASGIApp, create_base_app
from fastmcp.server.middleware import Middleware
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.routing import Route

from genesis.mcp.shared_execution import SharedExecution

CALLER = contextvars.ContextVar("caller", default="missing")


def message(method, request_id=None, params=None):
    value = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        value["id"] = request_id
    if params is not None:
        value["params"] = params
    return value


def headers(who, session=None):
    result = {"accept": "application/json, text/event-stream", "authorization": "Bearer " + who,
              "mcp-protocol-version": "2025-11-25"}
    if session:
        result["mcp-session-id"] = session
    return result


async def until(predicate, seconds=3):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.005)


class Auth(TokenVerifier):
    async def verify_token(self, token):
        if token in {"fixture-a", "fixture-b"}:
            return AccessToken(token=token, client_id="fixture", scopes=[], subject=token,
                               claims={"iss": "https://fixture.invalid"})
        return None


class Boundary(Middleware):
    def __init__(self):
        self.resets = []
        self.initializations = 0

    async def on_message(self, context, call_next):
        who = get_http_request().scope["user"].access_token.subject
        token = CALLER.set(who)
        self.initializations += context.method == "initialize"
        try:
            return await call_next(context)
        finally:
            CALLER.reset(token)
            self.resets.append((who, context.method, CALLER.get()))


async def main(mode, trigger, peer):
    signals = {who: {name: asyncio.Event() for name in
                    ("started", "cancelled", "finished", "release", "cleanup")}
               for who in ("fixture-a", "fixture-b")}
    counts = {"enter": 0, "exit": 0}

    @asynccontextmanager
    async def lifespan(server):
        counts["enter"] += 1
        try:
            yield {}
        finally:
            assert signals["fixture-a"]["finished"].is_set()
            counts["exit"] += 1

    server = FastMCP("execution-fixture", auth=Auth(), lifespan=lifespan)
    launcher = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/genesis_mcp_server.py"),
                             run_name="fixture_launcher")
    launcher["_suppress_docket_worker"](server)
    boundary = Boundary()
    server.add_middleware(boundary)

    @server.tool()
    async def hold() -> str:
        who = CALLER.get()
        state = signals[who]
        state["started"].set()
        try:
            await state["release"].wait()
            return who
        except asyncio.CancelledError:
            state["cancelled"].set()
            raise
        finally:
            assert CALLER.get() == who
            # Model a cancellation-protected resource finalizer. Real request-
            # database settlement is additionally qualified against the D floor.
            with anyio.CancelScope(shield=True):
                await state["cleanup"].wait()
            state["finished"].set()

    original = server._mcp_server
    run_before = original.run.__func__
    manager = StreamableHTTPSessionManager(SharedExecution(original), stateless=False,
        json_response=mode == "json", session_idle_timeout=.2 if trigger == "idle" else 60)
    @asynccontextmanager
    async def app_lifespan(app):
        async with server._lifespan_manager(), manager.run():
            yield
    endpoint = RequireAuthMiddleware(StreamableHTTPASGIApp(manager), [])
    app = create_base_app(routes=[Route("/mcp", endpoint, methods=["GET", "POST", "DELETE"])],
                          middleware=server.auth.get_middleware(), lifespan=app_lifespan)
    app.state.fastmcp_server = server
    app.state.path = "/mcp"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    runner = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False,
        lifespan="on", timeout_graceful_shutdown=.1))
    serving = asyncio.create_task(runner.serve(sockets=[sock]))
    calls = {}
    try:
        await until(lambda: runner.started)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=10) as http:
            sessions = {}
            for who in ("fixture-a", "fixture-b") if peer else ("fixture-a",):
                reply = await http.post("/mcp", headers=headers(who), json=message("initialize", 1, {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "fixture", "version": "1"}}))
                assert reply.status_code == 200
                sessions[who] = reply.headers["mcp-session-id"]
                reply = await http.post("/mcp", headers=headers(who, sessions[who]),
                                        json=message("notifications/initialized"))
                assert reply.status_code == 202
            for who in sessions:
                calls[who] = asyncio.create_task(http.post("/mcp", headers=headers(who, sessions[who]),
                    json=message("tools/call", 77, {"name": "hold", "arguments": {}})))
                await asyncio.wait_for(signals[who]["started"].wait(), 3)
            assert counts == {"enter": 1, "exit": 0}
            sid = sessions["fixture-a"]
            if trigger == "shutdown":
                runner.should_exit = True
            elif trigger == "delete":
                assert (await http.delete("/mcp", headers=headers("fixture-a", sid))).status_code == 200
            elif trigger == "idle":
                transport = manager._server_instances[sid]
                calls["fixture-a"].cancel()
                await asyncio.gather(calls["fixture-a"], return_exceptions=True)
                if mode == "json":
                    await asyncio.sleep(.45)
                    assert transport._requests_in_flight == 1 and sid in manager._server_instances
                    assert not signals["fixture-a"]["cancelled"].is_set()
                    trigger_cancel = True
                else:
                    trigger_cancel = False
                    await until(lambda: sid not in manager._server_instances)
            if trigger == "cancel" or (trigger == "idle" and mode == "json" and trigger_cancel):
                reply = await http.post("/mcp", headers=headers("fixture-a", sid),
                    json=message("notifications/cancelled", params={"requestId": 77}))
                assert reply.status_code == 202
            await asyncio.wait_for(signals["fixture-a"]["cancelled"].wait(), 3)
            assert not signals["fixture-a"]["finished"].is_set()
            assert counts["exit"] == 0
            if peer:
                if trigger == "shutdown":
                    await asyncio.wait_for(signals["fixture-b"]["cancelled"].wait(), 3)
                assert signals["fixture-b"]["cancelled"].is_set() == (trigger == "shutdown")
            for who in sessions:
                signals[who]["cleanup"].set()
            await asyncio.wait_for(signals["fixture-a"]["finished"].wait(), 3)
            if trigger == "shutdown":
                await asyncio.wait_for(asyncio.shield(serving), 4)
            elif peer:
                signals["fixture-b"]["release"].set()
                reply = await asyncio.wait_for(calls["fixture-b"], 3)
                assert reply.status_code == 200
                payloads = [json.loads(line[6:]) for line in reply.text.splitlines() if line.startswith("data: ")]
                payload = payloads[-1] if payloads else reply.json()
                assert payload["result"]["content"][0]["text"] == "fixture-b"
                assert not signals["fixture-b"]["cancelled"].is_set()
            await until(lambda: all((who, "tools/call", "missing") in boundary.resets for who in sessions))
            assert boundary.initializations == len(sessions)
    finally:
        for state in signals.values():
            state["cleanup"].set()
            state["release"].set()
        runner.should_exit = True
        await asyncio.wait_for(asyncio.shield(serving), 5)
        await asyncio.gather(*calls.values(), return_exceptions=True)
        sock.close()
    assert not manager._server_instances and not manager._session_owners
    assert counts == {"enter": 1, "exit": 1}
    assert server._mcp_server is original and original.run.__func__ is run_before
    print(json.dumps({"mode": mode, "trigger": trigger, "peer": peer, "passed": True}), flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], len(sys.argv) == 4))
