"""Disposable real P4 service for pipe/HTTP recovery and ownership tests."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

root, port = Path(sys.argv[1]), int(sys.argv[2])


def refuse(*args, **kwargs):
    raise PermissionError("forwarder backend refuses external effects")


socket.socket.connect = socket.socket.connect_ex = socket.getaddrinfo = refuse


class NoProcess(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        refuse()


subprocess.Popen = NoProcess

import aiosqlite  # noqa: E402
import uvicorn  # noqa: E402
from fastmcp import FastMCP  # noqa: E402
from fastmcp.server.dependencies import get_context  # noqa: E402

from genesis.caller_context import caller_context  # noqa: E402
from genesis.caller_credentials import CallerResolver  # noqa: E402
from genesis.db.request_database import RequestDatabase  # noqa: E402
from genesis.mcp.shared_http import SharedHTTPConfig, build_http_app  # noqa: E402
from genesis.mcp.shared_server import SharedServer  # noqa: E402
from genesis.observability.mcp_middleware import install_instrumentation  # noqa: E402
from genesis.observability.provider_activity import ProviderActivityTracker  # noqa: E402


def record(event, **values):
    with (root / "effects.jsonl").open("a") as stream:
        stream.write(json.dumps({"event": event, **values}) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


async def released(name):
    while not (root / name).exists():
        await asyncio.sleep(.01)


async def main():
    async with aiosqlite.connect(root / "synthetic.db") as connection:
        await connection.executescript(
            "CREATE TABLE IF NOT EXISTS update_history(status TEXT,completed_at TEXT,new_commit TEXT);"
            "CREATE TABLE IF NOT EXISTS activity_log(provider TEXT,latency_ms REAL,success INTEGER,"
            "cache_hit INTEGER,error_message TEXT,created_at TEXT);")
    db = RequestDatabase(root / "synthetic.db", max_units=2, max_background_units=1,
                         max_background_jobs=4)
    tracker = ProviderActivityTracker()
    tracker.set_db(db)
    server = FastMCP("forwarder-fixture")
    effect_entered = asyncio.Event()

    async def health_status(marker: str = "seed", wait: bool = False, payload_bytes: int = 0):
        await db.execute("SELECT 1")
        context = get_context()
        # Typed ID/meta observed at the actual SDK handler, independently of stdio.
        record("entered", marker=marker, request_id=context.request_id,
               metadata=context.request_context._srctx.meta,
               session_id=caller_context().session_id)
        if marker == "http-fault":
            effect_entered.set()
        try:
            if marker.startswith("callback-"):
                from mcp.shared.message import ServerMessageMetadata
                from mcp_types import ListRootsRequest, ListRootsResult

                metadata = (ServerMessageMetadata(related_request_id=context.request_id)
                            if marker == "callback-POST" else None)
                roots = await context.session.send_request(ListRootsRequest(), ListRootsResult,
                                                           metadata=metadata)
                return {"roots": roots.model_dump(mode="json", by_alias=True)["roots"]}
            if (context.request_context._srctx.meta or {}).get("progress_token") is not None:
                await context.report_progress(1, 2, "fixture")
            if wait:
                await released("release-" + marker)
            return {"marker": marker, "seed": 7, "payload": "x" * payload_bytes}
        except asyncio.CancelledError:
            record("cancelled", marker=marker)
            raise

    health_status.__module__ = "genesis.mcp.health.status"
    server.tool(health_status)
    install_instrumentation(server, tracker, "health", db)
    config = SharedHTTPConfig("health", f"http://127.0.0.1:{port}/mcp", 2, 2, 8, 30, 65536)
    application = await build_http_app(
        server, config=config, resolver=CallerResolver("f" * 43, {}), database=db, tracker=tracker,
        loaded_commit="a" * 40, started_at="2026-01-01T00:00:00+00:00", enabled=lambda: True)

    async def instrumented(scope, receive, send):
        corrupt, sent, sse, fault = False, False, False, None

        async def observe(message):
            nonlocal sent, sse
            if fault and message["type"] == "http.response.start":
                # Legacy headers can precede dispatch. Prove the effect happened
                # before exposing the abnormal response, rather than racing it.
                await effect_entered.wait()
                headers = [(key, value) for key, value in message["headers"]
                           if key not in {b"content-length", b"content-type"}]
                status = int(fault.removeprefix("redirect-")) if fault.startswith("redirect-") else 202 if fault == "accepted" else 200
                headers.append((b"content-type", b"text/plain"))
                if fault.startswith("redirect-"):
                    headers.append((b"location", b"/relocated"))
                record("fault-response", kind=fault)
                message = {**message, "status": status, "headers": headers}
            if corrupt and message["type"] == "http.response.start":
                sse = dict(message["headers"]).get(b"content-type", b"").startswith(b"text/event-stream")
                record("corrupt-response", kind="SSE" if sse else "JSON")
                message = {**message, "headers": [
                    (key, value) for key, value in message["headers"] if key != b"content-length"]}
            if corrupt and message["type"] == "http.response.body" and message.get("body"):
                if sent:
                    return
                sent = True
                # Preserve actual JSON vs SSE content type; a legacy-negotiated
                # response rewritten as JSON does not exercise the SSE parser.
                payload = b'{"fixture":"HARMLESS_PRIVATE_SENTINEL",'
                if sse:
                    payload = b"event: message\ndata: " + payload + b"\n\n"
                message = {"type": "http.response.body", "body": payload, "more_body": False}
            for line in message.get("body", b"").splitlines():
                if line.startswith(b"data: "):
                    try:
                        body = json.loads(line[6:])
                    except ValueError:
                        continue
                    if body.get("method") == "roots/list":
                        record("callback", request_id=body["id"], route=scope["method"])
            await send(message)

        if scope["type"] == "http":
            if scope["method"] == "DELETE" and (root / "hold-delete").exists():
                record("delete-entered")
                await released("release-delete")
            if scope["method"] == "POST":
                chunks, messages = [], []
                while True:
                    message = await receive()
                    messages.append(message)
                    chunks.append(message.get("body", b""))
                    if not message.get("more_body"):
                        break
                body = json.loads(b"".join(chunks))
                corrupt = body.get("params", {}).get("_meta", {}).get("fixture.corrupt", False)
                if scope["path"] == "/relocated":
                    scope = {**scope, "path": "/mcp", "raw_path": b"/mcp"}
                else:
                    fault = body.get("params", {}).get("_meta", {}).get("fixture.response")
                record("post", method=body.get("method"), request_id=body.get("id"))
                if body.get("method") == "initialize" and (root / "hold-init").exists():
                    record("initialize-entered")
                    await released("release-init")
                queued = iter(messages)
                original_receive = receive

                async def replay():
                    return next(queued, None) or await original_receive()

                receive = replay
        await application(scope, receive, observe)

    async def close():
        await db.close()
        record("owner-closed")

    runner = SharedServer(uvicorn.Config(
        instrumented, host="127.0.0.1", port=port, proxy_headers=False, access_log=False,
        log_level="warning", lifespan="on", timeout_graceful_shutdown=.25),
        database=db, grace_s=.25, close_resources=close)
    try:
        await runner.serve()
    finally:
        if runner.started:
            await runner.shutdown()


asyncio.run(main())
