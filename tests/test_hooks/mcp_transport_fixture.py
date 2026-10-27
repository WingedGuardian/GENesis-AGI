"""Synthetic localhost MCP subprocess. Never connect this fixture to real data."""

import argparse
import asyncio
import json
import os
import socket
from pathlib import Path

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from starlette.responses import PlainTextResponse


async def serve(fd: int | None, generation: int, stateless: bool, failure_mode: str):
    mcp = FastMCP("transport-fixture")

    @mcp.tool()
    def echo(value: str) -> dict:
        return {"value": value, "generation": generation, "pid": os.getpid(),
                "caller": get_http_headers().get("x-fixture-caller")}

    @mcp.tool()
    def commit(value: str) -> str:
        if failure_mode == "before":
            os._exit(71)  # Only this disposable fixture subprocess.
        with Path("writes.jsonl").open("a") as out:
            out.write(json.dumps({"value": value}) + "\n")
            out.flush()
            os.fsync(out.fileno())
        if failure_mode == "after":
            os._exit(72)  # Commit is durable; deliberately lose the reply.
        return "committed"

    @mcp.tool(name="count_writes" if generation == 3 else "read_count")
    def read_count() -> int:
        path = Path("writes.jsonl")
        return len(path.read_text().splitlines()) if path.exists() else 0

    if generation == 3:
        mcp.remove_tool("commit")

    if generation > 1:
        @mcp.tool()
        def new_tool() -> str:
            return "new catalog"

    if fd is None:
        await mcp.run_stdio_async(show_banner=False)
        return

    app = mcp.http_app(stateless_http=stateless, json_response=True)

    class Auth:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and dict(scope["headers"]).get(b"authorization") != b"Bearer fixture-local-only":
                await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
                return
            await app(scope, receive, send)

    server = uvicorn.Server(uvicorn.Config(Auth(), log_level="error", lifespan="on"))

    async def ready():
        while not server.started:
            await asyncio.sleep(0.01)
        print("READY", flush=True)

    task = asyncio.create_task(ready())
    try:
        await server.serve(sockets=[socket.socket(fileno=fd)])
    finally:
        task.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fd", type=int, help="Absent selects stdio")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--stateful", action="store_true")
    parser.add_argument("--failure-mode", choices=("normal", "before", "after"), default="normal")
    args = parser.parse_args()
    asyncio.run(serve(args.fd, args.generation, not args.stateful, args.failure_mode))
