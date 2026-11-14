"""A missing Scrapling must be loud, and an available one silent.

`scrapling.fetchers` imports browser-automation packages (playwright and
browserforge, among others) even for the plain AsyncFetcher. When one is
missing the import fails and web/fetch.py falls back to plain httpx, which
works but loses TLS impersonation; that used to happen with no trace. The fetch
tests patch `_HAS_SCRAPLING` off, which is why the logging needs tests of its own.
"""

from __future__ import annotations

import importlib
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

# `genesis.web` re-exports a function named `fetch`, so import the module by path.
fetch = importlib.import_module("genesis.web.fetch")


@pytest.fixture
def real_fetch_server():
    """Exercise the installed HTTP stack without external sites or browser profiles."""
    if not fetch._HAS_SCRAPLING:
        pytest.skip("real Scrapling integration requires the browser extra")
    import brotli

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = "café — 日本語".encode()
            status, content_type = 200, "text/plain; charset=utf-8"
            if self.path == "/html":
                body = b"<html><title>Fixture</title><body><p>Readable text</p></body></html>"
                content_type = "text/html; charset=utf-8"
            elif self.path == "/error":
                status = 503
            elif self.path == "/redirect":
                status = 302
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if self.path == "/brotli":
                body = brotli.compress(body)
                self.send_header("Content-Encoding", "br")
            if self.path == "/redirect":
                self.send_header("Location", "/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("path", ["plain", "html", "redirect", "brotli", "error"])
async def test_installed_scrapling_fetches_real_http(real_fetch_server, path):
    result = await fetch.WebFetcher(timeout_s=5).fetch(f"{real_fetch_server}/{path}")
    assert result.client == "scrapling", "fallback must not hide an import failure"
    if path == "error":
        assert result.status_code == 503
        assert result.error == "HTTP 503"
        assert not result.text
    else:
        assert result.status_code == 200
        assert result.error is None
        if path == "html":
            assert result.title == "Fixture"
            assert "Readable text" in result.text
        else:
            assert "café — 日本語" in result.text


def test_missing_scrapling_is_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(fetch, "_HAS_SCRAPLING", False)
    monkeypatch.setattr(
        fetch, "_SCRAPLING_IMPORT_ERROR", ImportError("No module named 'browserforge'")
    )
    monkeypatch.setattr(fetch, "_scrapling_failure_logged", False)
    with caplog.at_level(logging.WARNING, logger="genesis.web.fetch"):
        fetch.WebFetcher()
        fetch.WebFetcher()
    warnings = [r for r in caplog.records if "Scrapling is unavailable" in r.getMessage()]
    assert len(warnings) == 1
    assert "browserforge" in warnings[0].getMessage()
    # The warning states the fact and names no command. There is no supported,
    # deploy-locked way to add an optional extra to a live install, a package
    # install outside the deploy path changes the running checkout's
    # virtualenv, and the import result is cached until the process restarts.
    message = warnings[0].getMessage().lower()
    assert "install" not in message
    assert "scripts/" not in message


def test_import_failure_is_captured_at_import():
    """The tests above set `_SCRAPLING_IMPORT_ERROR` themselves; this one does not.

    A fresh interpreter with browserforge blocked imports the real module, so
    deleting the capture in the `except` branch fails here.
    """
    import os
    import subprocess
    import sys

    code = (
        "import sys; sys.modules['browserforge'] = None\n"
        "import importlib; f = importlib.import_module('genesis.web.fetch')\n"
        "assert f._HAS_SCRAPLING is False, 'scrapling imported despite the block'\n"
        "assert isinstance(f._SCRAPLING_IMPORT_ERROR, ImportError), f._SCRAPLING_IMPORT_ERROR\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    proc = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


def test_available_scrapling_logs_nothing(monkeypatch, caplog):
    monkeypatch.setattr(fetch, "_HAS_SCRAPLING", True)
    monkeypatch.setattr(fetch, "_scrapling_failure_logged", False)
    with caplog.at_level(logging.WARNING, logger="genesis.web.fetch"):
        fetch.WebFetcher()
    assert not [r for r in caplog.records if "Scrapling is unavailable" in r.getMessage()]
