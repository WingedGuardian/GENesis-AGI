"""Owned-network observers for the real ordinary/shared memory bootstraps."""

import functools
import json
import os
import socket
import subprocess
import sys
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path


def install():
    port = int(os.environ["LOCAL_MEMORY_TEST_PORT"])
    report = Path(os.environ["LOCAL_MEMORY_TEST_REPORT"])
    role = os.environ["LOCAL_MEMORY_TEST_ROLE"]
    root = Path(__file__).resolve().parents[2]

    def record(event, **fields):
        with report.open("a") as output:
            output.write(json.dumps({"event": event, "role": role, **fields}) + "\n")

    real_connect, real_resolve = socket.socket.connect, socket.getaddrinfo

    def connect(sock, address):
        if address != ("127.0.0.1", port):
            raise PermissionError("memory fixture refuses non-owned network")
        return real_connect(sock, address)

    def resolve(host, target, *args, **kwargs):
        if (host, target) != ("127.0.0.1", port):
            raise PermissionError("memory fixture refuses non-owned resolution")
        return real_resolve(host, target, *args, **kwargs)

    socket.socket.connect = socket.socket.connect_ex = connect
    socket.getaddrinfo = resolve
    original_popen = subprocess.Popen

    class IdentityGitOnly(original_popen):
        def __init__(self, argv, *args, **kwargs):
            if argv != ["git", "-C", str(root), "rev-parse", "HEAD"]:
                raise PermissionError("memory fixture refuses non-identity subprocesses")
            super().__init__(argv, *args, **kwargs)

    subprocess.Popen = IdentityGitOnly

    import httpx2
    import qdrant_client
    from fastmcp import FastMCP
    from fastmcp.client.transports.memory import FastMCPTransport
    from fastmcp.server.dependencies import get_context

    actual_qdrant = qdrant_client.QdrantClient

    class LocalQdrant(actual_qdrant):
        def __init__(self, *args, **kwargs):
            super().__init__(":memory:")
            from genesis.qdrant.collections import ensure_collections

            ensure_collections(self)

    qdrant_client.QdrantClient = LocalQdrant
    original_send = httpx2.AsyncClient.send

    async def send(client, request, *args, **kwargs):
        if request.url.host == "127.0.0.1" and request.url.port == port:
            record("http-request", method=request.method, path=request.url.path,
                   body=json.loads(request.content) if request.content else None)
        return await original_send(client, request, *args, **kwargs)

    httpx2.AsyncClient.send = send
    real_init = FastMCP.__init__

    def init(server, *args, **kwargs):
        kwargs.setdefault("list_page_size", 7)
        real_init(server, *args, **kwargs)

    FastMCP.__init__ = init
    from genesis.mcp import memory, shared_settings
    from tests.test_mcp.local_memory_data import providers, seed

    providers(record)
    if role == "backend" and os.environ.get("LOCAL_MEMORY_TEST_SEED") == "1":
        from genesis.mcp import shared_resources

        original_resources = shared_resources.initialize_shared_service

        @asynccontextmanager
        async def seeded_resources(*args, **kwargs):
            async with original_resources(*args, **kwargs) as initialized:
                await seed(memory, record)
                yield initialized
                record("vectors-before-close", episodic=memory._qdrant.get_collection("episodic_memory").points_count)

        shared_resources.initialize_shared_service = seeded_resources

    original_program = shared_settings._rollout_program

    @contextmanager
    def program(path):
        with original_program(path) as module:
            module.MAX_STAGE = "health-reads" if os.environ["LOCAL_MEMORY_TEST_MIXED"] == "1" else "off"

            def ready(unit):
                record("readiness", unit=unit)
                return True, "running"

            module.unit_ready = ready
            yield module

    shared_settings._rollout_program = program
    original_memory_init = memory.init

    @functools.wraps(original_memory_init)
    def memory_init(**kwargs):
        record("memory-init", pending_enabled=kwargs.get("process_pending_bookmarks", True))
        return original_memory_init(**kwargs)

    memory.init = memory_init
    original_guard = memory._require_init

    @functools.wraps(original_guard)
    def guard():
        context = get_context().request_context
        meta = context.meta.model_dump(by_alias=True, mode="json") if hasattr(context.meta, "model_dump") else dict(context.meta or {})
        record("body-guard", request_id=context._srctx.request_id, metadata=meta)
        return original_guard()

    memory._require_init = guard

    def observe_lifespan(server):
        original = server._lifespan

        class ObserveLifespan:
            def __init__(self, instance):
                self.manager = original(instance)

            async def __aenter__(self):
                state = await self.manager.__aenter__()
                record("lifespan-enter")
                return state

            async def __aexit__(self, *exc):
                record("vectors-before-close", episodic=memory._qdrant.get_collection("episodic_memory").points_count)
                result = await self.manager.__aexit__(*exc)
                record("lifespan-returned")
                return result

        server._lifespan = ObserveLifespan
        return original

    original_run = FastMCP.run

    def run(server, *args, **kwargs):
        original = observe_lifespan(server)
        try:
            return original_run(server, *args, **kwargs)
        finally:
            server._lifespan = original

    FastMCP.run = run
    original_connect = FastMCPTransport.connect_session

    @asynccontextmanager
    async def observed_connect(transport, **kwargs):
        original = observe_lifespan(transport.server)
        try:
            async with original_connect(transport, **kwargs) as session:
                yield session
        finally:
            transport.server._lifespan = original

    FastMCPTransport.connect_session = observed_connect
    record("source", argv=sys.argv)
