"""Disposable dependencies for subprocess tests of the real MCP launchers.

Loaded by a test-owned sitecustomize, never by production startup. Real server
bootstrap/lifespan/dispatch code runs against SQLite and Qdrant's local mode.
Every network connection is denied; Discord and peer HTTP use MockTransport.
"""

from __future__ import annotations

import socket


def install():
    import json
    import os
    from contextlib import asynccontextmanager
    from importlib.metadata import version
    from pathlib import Path

    def deny_network(*args, **kwargs):
        raise AssertionError("network disabled in disposable MCP fixture")

    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.getaddrinfo = deny_network

    import httpx
    import qdrant_client
    from fastmcp import FastMCP
    from fastmcp.server.middleware import Middleware

    report_path = Path(os.environ["GENESIS_STDIO_TEST_REPORT"])
    report = {"fastmcp": version("fastmcp"), "mcp": version("mcp"),
              "entries": 0, "exits": 0, "listed_without_worker": False}

    def save():
        report_path.write_text(json.dumps(report))

    real_run = FastMCP.run

    def run(server, *args, **kwargs):
        import genesis
        from genesis.env import genesis_db_path, genesis_home, repo_root

        assert genesis_db_path() == Path(os.environ["GENESIS_DB_PATH"])
        assert genesis_home() == Path(os.environ["GENESIS_HOME"])
        assert repo_root() == Path(os.environ["GENESIS_REPO_ROOT"])
        report["source"] = str(Path(genesis.__file__).resolve())
        original_lifespan = server._lifespan

        @asynccontextmanager
        async def lifespan(instance):
            report["entries"] += 1
            save()
            try:
                async with original_lifespan(instance) as state:
                    yield state
            finally:
                report["exits"] += 1
                save()

        class Probe(Middleware):
            async def on_list_tools(self, context, call_next):
                tools = await call_next(context)
                assert server._docket is None and server._worker is None
                report["listed_without_worker"] = True
                save()
                return tools

        server._lifespan = lifespan
        server.add_middleware(Probe())
        return real_run(server, *args, **kwargs)

    FastMCP.run = run

    real_qdrant = qdrant_client.QdrantClient

    class LocalQdrant(real_qdrant):
        def __init__(self, *args, **kwargs):
            super().__init__(":memory:")

    qdrant_client.QdrantClient = LocalQdrant

    def response(request):
        if request.url.host == "discord.com":
            return httpx.Response(200, json=[])
        if request.url.host == "peer.example.invalid":
            return httpx.Response(200, json={"status": "fixture"})
        raise AssertionError("unexpected HTTP request in disposable MCP fixture")

    real_client = httpx.AsyncClient

    class LocalHTTPClient(real_client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **{**kwargs, "transport": httpx.MockTransport(response)})

    httpx.AsyncClient = LocalHTTPClient
