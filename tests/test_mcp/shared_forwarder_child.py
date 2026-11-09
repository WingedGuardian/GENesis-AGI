"""Run the actual forwarder CLI with only its disposable endpoint reachable."""

import logging
import os
import runpy
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
service, port = sys.argv[1], int(sys.argv[2])
connect, resolve = socket.socket.connect, socket.getaddrinfo


def owned_connect(sock, address):
    if address != ("127.0.0.1", port):
        raise PermissionError("forwarder fixture refuses non-owned network")
    return connect(sock, address)


def owned_resolve(host, target, *args, **kwargs):
    if (host, target) != ("127.0.0.1", port):
        raise PermissionError("forwarder fixture refuses non-owned resolution")
    return resolve(host, target, *args, **kwargs)


socket.socket.connect = socket.socket.connect_ex = owned_connect
socket.getaddrinfo = owned_resolve


class NoProcess(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        raise PermissionError("forwarder fixture refuses subprocesses")


subprocess.Popen = NoProcess

import httpx2  # noqa: E402

if os.environ.get("FORWARDER_FIXTURE_DROP_CANCEL"):
    from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher

    original_send = JSONRPCDispatcher.send_raw_request

    async def no_courtesy(self, method, params=None, opts=None):
        return await original_send(self, method, params, {**(opts or {}), "cancel_on_abandon": False})

    JSONRPCDispatcher.send_raw_request = no_courtesy

original_client = httpx2.AsyncClient


class ObservedClient(original_client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # These must miss the wire after the actual CLI has claimed stdio.
        os.write(1, b"FIXTURE_STRAY_STDOUT\n")
        assert os.read(0, 1) == b""
        logging.getLogger("mcp.client.streamable_http").error("FIXTURE_SENSITIVE_SDK_DIAGNOSTIC")


httpx2.AsyncClient = ObservedClient
sys.argv = [str(ROOT / "scripts/genesis_mcp_forwarder.py"), "--server", service,
            "--url", f"http://127.0.0.1:{port}/mcp"]
runpy.run_path(sys.argv[0], run_name="__main__")
