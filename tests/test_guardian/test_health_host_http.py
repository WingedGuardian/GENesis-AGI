"""Exercise Guardian's real HTTP client independently of its ICMP target."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import AsyncMock, patch

import pytest

from genesis.guardian.config import GuardianConfig
from genesis.guardian.dialogue import DialogueRequest, send_dialogue
from genesis.guardian.health_signals import probe_health_api, probe_icmp_reachable


@pytest.mark.asyncio
async def test_loopback_http_probe_preserves_container_ping(monkeypatch):
    for name in (
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = GuardianConfig(
                container_ip="192.0.2.1",
                health_api_host="127.0.0.1",
                health_api_port=server.server_port,
            )
            result = await probe_health_api(config)
            assert result.alive, result.detail
            assert requests == ["/api/genesis/health"]
            ping = AsyncMock(return_value=(0, "reachable", ""))
            with patch("genesis.guardian.health_signals._run_subprocess", ping):
                assert (await probe_icmp_reachable(config)).alive
            assert ping.call_args.args[-1] == "192.0.2.1"
            assert config.container_ip == "192.0.2.1"
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.asyncio
async def test_icmp_autodetection_is_independent_of_http_override():
    config = GuardianConfig(health_api_host="127.0.0.1")
    ping = AsyncMock(return_value=(0, "reachable", ""))
    with (
        patch.object(config, "_detect_container_ip", return_value="192.0.2.2") as detect,
        patch("genesis.guardian.health_signals._run_subprocess", ping),
    ):
        assert config.health_url == "http://127.0.0.1:5000"
        assert (await probe_icmp_reachable(config)).alive
    detect.assert_called_once_with()
    assert ping.call_args.args[-1] == "192.0.2.2"


@pytest.mark.asyncio
async def test_failed_autodetection_never_pings_host_loopback():
    config = GuardianConfig(health_api_host="127.0.0.1")
    ping = AsyncMock(return_value=(0, "would hide failure", ""))
    with (
        patch.object(config, "_detect_container_ip", return_value="127.0.0.1"),
        patch("genesis.guardian.health_signals._run_subprocess", ping),
    ):
        assert not (await probe_icmp_reachable(config)).alive
    ping.assert_not_called()


@pytest.mark.asyncio
async def test_health_and_dialogue_ignore_environment_proxy(tmp_path, monkeypatch):
    from contextlib import ExitStack

    target_requests, proxy_requests = [], []
    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            target_requests.append(("GET", self.path))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')
        def do_POST(self):
            target_requests.append(("POST", self.path))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"acknowledged":true,"status":"handling","action":"check","eta_s":1,"context":"fixture"}')
        def log_message(self, *args):
            pass
    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            proxy_requests.append("GET")
            self.send_response(502)
            self.end_headers()
        def do_POST(self):
            proxy_requests.append("POST")
            self.send_response(502)
            self.end_headers()
        def log_message(self, *args):
            pass
    with ExitStack() as stack:
        target = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), Target))
        proxy = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), Proxy))
        threads = [Thread(target=server.serve_forever, daemon=True) for server in (target, proxy)]
        for thread in threads:
            thread.start()
        try:
            for name in ("http_proxy", "HTTP_PROXY"):
                monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.server_port}")
            for name in ("no_proxy", "NO_PROXY"):
                monkeypatch.setenv(name, "")
            config = GuardianConfig(health_api_host="127.0.0.1", health_api_port=target.server_port, state_dir=tmp_path)
            with patch("genesis.guardian.credential_bridge.load_internal_api_token", return_value=None):
                assert (await probe_health_api(config)).alive
                response = await send_dialogue(config, DialogueRequest([], [], 1, "HEALTHY", {}))
            assert response.acknowledged and response.action == "check"
            assert target_requests == [("GET", "/api/genesis/health"), ("POST", "/api/genesis/guardian-dialogue")]
            assert proxy_requests == []
        finally:
            for server in (target, proxy):
                server.shutdown()
            for thread in threads:
                thread.join(timeout=5)
                assert not thread.is_alive()
