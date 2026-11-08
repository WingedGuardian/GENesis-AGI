"""Synthetic catalog mutation with evidence of the actual outbound SSE notification."""

import argparse
import asyncio
import json
import socket

import uvicorn
from fastmcp import Context, FastMCP
from mcp import types
from starlette.responses import PlainTextResponse


async def serve(fd, stateless):
    mcp = FastMCP("notification-fixture")
    observed = {"notifications_sent": 0, "tools_list_requests": 0}

    @mcp.tool()
    def audit() -> dict:
        return dict(observed)

    @mcp.tool()
    def echo() -> str:
        return "healthy"

    @mcp.tool()
    async def change_catalog(ctx: Context) -> str:
        @mcp.tool()
        def new_tool() -> str:
            return "new catalog"

        # The SDK helper sends on the optional standalone GET stream. Associate
        # this protocol notification with the current request so both stateful
        # and stateless clients receive it over this request's SSE response.
        await ctx.session.send_notification(
            types.ServerNotification(types.ToolListChangedNotification()),
            related_request_id=ctx.request_id,
        )
        return "catalog changed"

    app = mcp.http_app(stateless_http=stateless, json_response=False)

    class ObserveWire:
        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await app(scope, receive, send)
            if dict(scope["headers"]).get(b"authorization") != b"Bearer fixture-local-only":
                return await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
            incoming, outgoing = bytearray(), bytearray()

            async def read():
                message = await receive()
                if message["type"] == "http.request":
                    incoming.extend(message.get("body", b""))
                    if not message.get("more_body") and incoming:
                        body = json.loads(incoming)
                        if body.get("method") == "tools/list":
                            observed["tools_list_requests"] += 1
                        incoming.clear()
                return message

            async def write(message):
                await send(message)
                if message["type"] == "http.response.body":
                    outgoing.extend(message.get("body", b""))
                    while b"\n" in outgoing:
                        line, _, tail = outgoing.partition(b"\n")
                        outgoing[:] = tail
                        if line.startswith(b"data: "):
                            data = line[6:].strip()
                            if data and json.loads(data).get("method") == "notifications/tools/list_changed":
                                observed["notifications_sent"] += 1

            await app(scope, read, write)

    server = uvicorn.Server(uvicorn.Config(ObserveWire(), log_level="error", lifespan="on"))

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
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--stateful", action="store_true")
    parser.add_argument("--generation", type=int, choices=(1,), default=1)
    parser.add_argument("--failure-mode", choices=("normal",), default="normal")
    args = parser.parse_args()
    asyncio.run(serve(args.fd, args.stateful is False))
