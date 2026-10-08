"""Exercise Guardian's real HTTP client independently of its ICMP target."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import AsyncMock, patch

import pytest

from genesis.guardian.config import GuardianConfig
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
