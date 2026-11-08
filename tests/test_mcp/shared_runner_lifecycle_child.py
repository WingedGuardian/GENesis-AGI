"""Disposable runner process: real Uvicorn, inert resource/ASGI dependencies."""

import asyncio
import hashlib
import json
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import uvicorn

from genesis.mcp import shared_server

case, receipt = sys.argv[1], Path(sys.argv[2])
state = {"case": case, "events": [], "running": False,
         "source_sha256": hashlib.sha256(Path(shared_server.__file__).read_bytes()).hexdigest()}


def record(event):
    state["events"].append(event)
    receipt.write_text(json.dumps(state))


class Database:
    def begin_draining(self):
        record("admission-closed")

    async def drain(self, **kwargs):
        record("drain-confirmed")


@asynccontextmanager
async def factory(*args, **kwargs):
    record("owner-entered")
    try:
        yield SimpleNamespace(server=None, database=Database(), tracker=None)
    finally:
        record("owner-closed-running=" + str(state["running"]))
        await asyncio.sleep(0)
        record("resources-closed")


async def app(scope, receive, send):
    assert scope["type"] == "lifespan"
    assert (await receive())["type"] == "lifespan.startup"
    state["running"] = True
    record("lifespan-started")
    if case == "lifespan-failed":
        state["running"] = False
        record("lifespan-stopped")
        await send({"type": "lifespan.startup.failed"})
        return
    if case == "lifespan-cancelled":
        asyncio.get_running_loop().call_soon(owner_task.cancel)
        await asyncio.sleep(0)
    await send({"type": "lifespan.startup.complete"})
    assert (await receive())["type"] == "lifespan.shutdown"
    state["running"] = False
    record("lifespan-stopped")
    await send({"type": "lifespan.shutdown.complete"})


async def build(*args, **kwargs):
    return app


async def lifespan_shutdown():
    if state["running"]:
        await asyncio.sleep(.03)
        state["running"] = False
        record("lifespan-stopped")


async def running_serve(self, sockets=None):
    self.started = True
    self.servers = []
    self.lifespan = SimpleNamespace(shutdown=lifespan_shutdown)
    state["running"] = True
    record("lifespan-started")
    if case in {"SIGTERM", "SIGINT"}:
        os.kill(os.getpid(), getattr(signal, case))
    elif case == "shutdown-cancelled":
        asyncio.get_running_loop().call_later(.11, asyncio.current_task().cancel)
    elif case == "force-exit":
        self.force_exit = True
    await self.shutdown()


async def main():
    global owner_task
    owner_task = asyncio.current_task()
    if case in {"bind-cancelled", "bind-error", "lifespan-cancelled", "lifespan-failed"}:
        async def fail_bind(*args, **kwargs):
            record("bind-attempt")
            if case == "bind-error":
                raise OSError("synthetic bind failure")
            raise asyncio.CancelledError()

        asyncio.get_running_loop().create_server = fail_bind
    else:
        uvicorn.Server._serve = running_serve
    shared_server.initialize_shared_service = factory
    shared_server.build_http_app = build
    shared_server.enter_shared_service = lambda: None
    settings = SimpleNamespace(http=SimpleNamespace(url="http://127.0.0.1:1/mcp"), shutdown_grace_s=.25)
    await shared_server.serve_shared(settings, resolver=None, loaded_commit="a" * 40,
        started_at="2026-11-08T00:00:00+00:00", db_path=None, status_path=None,
        cache_dir=None, enabled=lambda: True, setup_routing=None, setup_health_actions=None)


try:
    asyncio.run(main())
except BaseException as error:
    record(type(error).__name__)
