"""Test-owned injection for actual ordinary health argv and owned HTTP only."""

import hashlib
import json
import os
import socket
import subprocess
import sys
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path


def install():
    port = int(os.environ["LOCAL_HEALTH_TEST_PORT"])
    report = Path(os.environ["LOCAL_HEALTH_TEST_REPORT"])
    role = os.environ["LOCAL_HEALTH_TEST_ROLE"]
    root = Path(__file__).resolve().parents[2]
    active = 0

    def record(event, **fields):
        with report.open("a") as output:
            output.write(json.dumps({"event": event, "role": role, **fields}) + "\n")

    real_connect, real_resolve = socket.socket.connect, socket.getaddrinfo

    def connect(sock, address):
        if address != ("127.0.0.1", port):
            raise PermissionError("health fixture refuses non-owned network")
        return real_connect(sock, address)

    def resolve(host, target, *args, **kwargs):
        if (host, target) != ("127.0.0.1", port):
            raise PermissionError("health fixture refuses non-owned resolution")
        return real_resolve(host, target, *args, **kwargs)

    socket.socket.connect = socket.socket.connect_ex = connect
    socket.getaddrinfo = resolve

    original_popen = subprocess.Popen

    class IdentityGitOnly(original_popen):
        def __init__(self, argv, *args, **kwargs):
            if argv != ["git", "-C", str(root), "rev-parse", "HEAD"]:
                raise PermissionError("health fixture refuses non-identity subprocesses")
            super().__init__(argv, *args, **kwargs)

    subprocess.Popen = IdentityGitOnly

    import httpx2
    from fastmcp import FastMCP
    from fastmcp.client.transports.memory import FastMCPTransport
    from fastmcp.server.dependencies import get_context
    from starlette.responses import JSONResponse

    original_send = httpx2.AsyncClient.send

    async def send(client, request, *args, **kwargs):
        if request.url.host == "127.0.0.1" and request.url.port == port:
            record("http-request", method=request.method, path=request.url.path,
                   protocol=request.headers.get("mcp-protocol-version"),
                   body=json.loads(request.content) if request.content else None)
        return await original_send(client, request, *args, **kwargs)

    httpx2.AsyncClient.send = send

    real_init = FastMCP.__init__

    def init(server, *args, **kwargs):
        kwargs.setdefault("list_page_size", 7)  # Public construction option.
        real_init(server, *args, **kwargs)

    FastMCP.__init__ = init
    from genesis.mcp import shared_settings
    from genesis.mcp.health import browser, status

    original_program = shared_settings._rollout_program

    @contextmanager
    def program(path):
        with original_program(path) as module:
            module.MAX_STAGE = "health-reads" if os.environ.get("LOCAL_HEALTH_TEST_MIXED") == "1" else "off"

            def ready(unit):
                record("readiness", unit=unit)
                return True, "loaded/enabled/active/running"

            module.unit_ready = ready
            yield module

    shared_settings._rollout_program = program
    original_status = status._impl_health_status

    def context_fields():
        rc = get_context().request_context
        meta = rc.meta.model_dump(by_alias=True, mode="json") if hasattr(rc.meta, "model_dump") else dict(rc.meta or {})
        # Receipt observer only; production keeps the native ID in its public frame.
        return {"id": rc._srctx.request_id, "meta": meta}

    async def health():
        record("body", tool="health_status", **context_fields())
        return await original_status()

    async def fill(selector, value):
        nonlocal active
        active += 1
        record("body", tool="browser_fill", selector=selector, value=value, **context_fields())
        try:
            await browser._fill_progress.get()("fixture local progress")
            if value == "hold":
                import anyio

                await anyio.sleep_forever()
            return {"filled": selector, "value": value}
        finally:
            active -= 1
            record("handler-finished", active=active)

    status._impl_health_status = health
    browser._impl_browser_fill = fill
    browser._FILL_PROGRESS_EVERY_S = 0
    original_cleanup = browser.async_cleanup

    async def cleanup():
        await original_cleanup()
        record("browser-cleanup-returned", active=active)

    browser.async_cleanup = cleanup
    original_connect = FastMCPTransport.connect_session

    @asynccontextmanager
    async def observed_connect(transport, **kwargs):
        server = transport.server
        original_lifespan = server._lifespan

        class ObserveLifespan:
            def __init__(self, instance):
                self.manager = original_lifespan(instance)

            async def __aenter__(self):
                state = await self.manager.__aenter__()
                record("lifespan-enter")
                return state

            async def __aexit__(self, *exc):
                result = await self.manager.__aexit__(*exc)
                record("lifespan-returned", active=active)
                return result

        server._lifespan = ObserveLifespan
        try:
            async with original_connect(transport, **kwargs) as session:
                yield session
        finally:
            server._lifespan = original_lifespan

    FastMCPTransport.connect_session = observed_connect
    original_render = JSONResponse.render

    def render(response, content):
        state_file = os.environ.get("LOCAL_HEALTH_TEST_REVISION_STATE")
        bad_revision = os.environ.get("LOCAL_HEALTH_TEST_BAD_REVISION") == "1" or (
            state_file is not None and Path(state_file).read_text() == "bad"
        )
        if (role == "backend" and bad_revision
                and isinstance(content, dict) and "catalog_revision" in content):
            content = {**content, "catalog_revision": "0" * 64}
        return original_render(response, content)

    JSONResponse.render = render
    record("source", argv=sys.argv, script_sha256=hashlib.sha256(
        (root / "scripts/genesis_mcp_server.py").read_bytes()).hexdigest())
