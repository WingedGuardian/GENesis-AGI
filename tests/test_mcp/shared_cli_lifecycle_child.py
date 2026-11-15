"""Real shared CLI with disposable dependencies and lifetime observers only."""

import importlib.abc
import importlib.util
import json
import os
import runpy
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
service, port, events = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
real_connect, real_resolve = socket.socket.connect, socket.getaddrinfo


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
from genesis.mcp import shared_server  # noqa: E402
from genesis.memory.embeddings import EmbeddingProvider  # noqa: E402
from genesis.memory.reranker import VoyageReranker  # noqa: E402


class LocalQdrant(qdrant_client.QdrantClient):
    def __init__(self, **kwargs):
        super().__init__(":memory:")

    def close(self, *args, **kwargs):
        super().close(*args, **kwargs)
        record("qdrant-closed")


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
actual_factory = shared_server.initialize_shared_service


@asynccontextmanager
async def factory(*args, **kwargs):
    async with actual_factory(*args, **kwargs) as resources:
        yield resources
    record("factory-exited")


shared_server.initialize_shared_service = factory
sys.argv = [str(ROOT / "scripts/genesis_mcp_server.py"), "--shared-service", service,
            "--transport", "streamable-http"]
runpy.run_path(sys.argv[0], run_name="__main__")
