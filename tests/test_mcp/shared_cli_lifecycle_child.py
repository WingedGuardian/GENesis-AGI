"""Real shared CLI with disposable dependencies and lifetime observers only."""

import asyncio
import importlib.abc
import importlib.util
import json
import os
import runpy
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
service, port, events = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
notify_case = sys.argv[4] if len(sys.argv) > 4 else "normal"
notify_address = os.environ.get("NOTIFY_SOCKET", "")
notify_address = "\0" + notify_address[1:] if notify_address.startswith("@") else notify_address
real_connect, real_resolve = socket.socket.connect, socket.getaddrinfo
real_sendto = socket.socket.sendto


def permitted(address):
    if address != ("127.0.0.1", port):
        raise PermissionError("shared CLI fixture refuses non-owned network")


def connect(sock, address):
    permitted(address)
    return real_connect(sock, address)


def resolve(host, target, *args, **kwargs):
    permitted((host, target))
    return real_resolve(host, target, *args, **kwargs)


socket.socket.connect = connect
socket.socket.connect_ex = connect
socket.getaddrinfo = resolve


class OnlyIdentityGit(subprocess.Popen):
    def __init__(self, argv, *args, **kwargs):
        if argv != ["git", "-C", str(ROOT), "rev-parse", "HEAD"]:
            raise PermissionError("shared CLI fixture refuses non-identity subprocess")
        super().__init__(argv, *args, **kwargs)


subprocess.Popen = OnlyIdentityGit


def record(event):
    with events.open("a") as out:
        out.write(json.dumps(event) + "\n")


def sendto(sock, data, address):
    if sock.family != socket.AF_UNIX or address != notify_address or data != b"READY=1":
        raise PermissionError("shared CLI fixture refuses non-owned datagram")
    sent = real_sendto(sock, data, address)
    record("ready-sent")
    return sent


socket.socket.sendto = sendto


# A private old-source control never changes the checkout or installed SDK.
if source := os.environ.get("GENESIS_SHARED_CLI_SERVER_SOURCE"):
    class PrivateRunner(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, name, path=None, target=None):
            if name == "genesis.mcp.shared_server":
                return importlib.util.spec_from_loader(name, self)
            return None

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            module.__file__ = str(ROOT / "src/genesis/mcp/shared_server.py")
            exec(compile(Path(source).read_bytes(), source, "exec"), module.__dict__)  # noqa: S102

    sys.meta_path.insert(0, PrivateRunner())

import qdrant_client  # noqa: E402
from uvicorn.lifespan.on import LifespanOn  # noqa: E402

from genesis.db.request_database import RequestDatabase  # noqa: E402
from genesis.mcp import shared_resources, shared_server  # noqa: E402
from genesis.memory.embeddings import EmbeddingProvider  # noqa: E402
from genesis.memory.reranker import VoyageReranker  # noqa: E402

actual_init = shared_server.SharedServer.__init__
server_instance = None


def server_init(self, *args, **kwargs):
    global server_instance
    actual_init(self, *args, **kwargs)
    server_instance = self


shared_server.SharedServer.__init__ = server_init
actual_bind = asyncio.BaseEventLoop.create_server


async def bind(loop, *args, **kwargs):
    listener = await actual_bind(loop, *args, **kwargs)
    record("bound")
    if notify_case == "stopping-during-bind":
        server_instance.should_exit = True
    elif notify_case == "notify-missing":
        # An omitted/swallowed notify still exits deterministically, rather
        # than turning that negative control into a timeout.
        loop.call_soon(setattr, server_instance, "should_exit", True)
    return listener


asyncio.BaseEventLoop.create_server = bind


class LocalQdrant(qdrant_client.QdrantClient):
    def __init__(self, **kwargs):
        super().__init__(":memory:")

    def close(self, *args, **kwargs):
        super().close(*args, **kwargs)
        record("qdrant-finalizer-closed" if getattr(self, "_observer_finalizer", False) else "qdrant-closed")

    def __del__(self):
        # Preserve the actual SDK finalizer and distinguish it from owner
        # cleanup. Duplicate owner calls still produce duplicate owner events.
        self._observer_finalizer = True
        try:
            super().__del__()
        finally:
            self._observer_finalizer = False


qdrant_client.QdrantClient = LocalQdrant
actual_drain = RequestDatabase.drain


async def drain(self, **kwargs):
    await actual_drain(self, **kwargs)
    record("drain-confirmed")


RequestDatabase.drain = drain
actual_cache_close = EmbeddingProvider.close_cache


def cache_close(self):
    actual_cache_close(self)
    record("cache-closed")


EmbeddingProvider.close_cache = cache_close
actual_reranker_close = VoyageReranker.close


async def reranker_close(self):
    await actual_reranker_close(self)
    record("reranker-closed")


VoyageReranker.close = reranker_close
actual_lifespan_shutdown = LifespanOn.shutdown


async def lifespan_shutdown(self):
    await actual_lifespan_shutdown(self)
    record("lifespan-stopped")


LifespanOn.shutdown = lifespan_shutdown
actual_close_resources = shared_resources.close_owned_resources


async def close_resources(*args, **kwargs):
    # A context-manager wrapper's normal continuation is skipped when startup
    # raises, even after cleanup succeeded. Observe the actual cleanup return.
    await actual_close_resources(*args, **kwargs)
    record("resources-closed")


shared_resources.close_owned_resources = close_resources
sys.argv = [str(ROOT / "scripts/genesis_mcp_server.py"), "--shared-service", service,
            "--transport", "streamable-http"]
runpy.run_path(sys.argv[0], run_name="__main__")
