"""Tests for the _async_route bridge — esp. the opt-in timeout backstop.

The dashboard's async routes dispatch their coroutine onto the runtime event
loop via run_coroutine_threadsafe and block the Flask worker thread on
future.result(). Without a timeout, a slow/contended snapshot makes the
endpoint hang indefinitely (the dashboard "spins forever" and the host
Guardian's health probe times out). The opt-in timeout returns 503 instead.
"""
from __future__ import annotations

import asyncio
import threading
import time

from flask import Flask

from genesis.dashboard._blueprint import _async_route


def _running_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()), daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while not loop.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    return loop


def test_async_route_timeout_returns_503():
    """A handler that exceeds its timeout returns 503, not a hang."""
    loop = _running_loop()
    app = Flask(__name__)
    app.config["GENESIS_EVENT_LOOP"] = loop

    @_async_route(timeout=0.2)
    async def slow():
        await asyncio.sleep(5)
        return "should not reach"

    try:
        start = time.monotonic()
        with app.test_request_context("/"):
            body, status = slow()
        elapsed = time.monotonic() - start
        assert status == 503
        assert elapsed < 2  # returned promptly on timeout, did not wait 5s
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_async_route_no_timeout_completes_normally():
    """Bare @_async_route (no timeout) preserves unbounded behavior."""
    loop = _running_loop()
    app = Flask(__name__)
    app.config["GENESIS_EVENT_LOOP"] = loop

    @_async_route
    async def fast():
        await asyncio.sleep(0.01)
        return "ok"

    try:
        with app.test_request_context("/"):
            assert fast() == "ok"
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_async_route_timeout_allows_fast_handler():
    """A handler well under its timeout returns its value normally."""
    loop = _running_loop()
    app = Flask(__name__)
    app.config["GENESIS_EVENT_LOOP"] = loop

    @_async_route(timeout=5)
    async def quick():
        await asyncio.sleep(0.01)
        return "fine"

    try:
        with app.test_request_context("/"):
            assert quick() == "fine"
    finally:
        loop.call_soon_threadsafe(loop.stop)
def test_async_route_configured_but_stopped_loop_returns_503():
    """A configured-but-not-running loop (startup window) -> 503, never a
    fresh event loop running the handler against cross-loop state."""
    loop = asyncio.new_event_loop()  # configured, NOT running
    app = Flask(__name__)
    app.config["GENESIS_EVENT_LOOP"] = loop

    ran = []

    @_async_route
    async def handler():
        ran.append(True)
        return "should not run"

    try:
        with app.test_request_context("/"):
            resp, status = handler()
        assert status == 503
        assert resp.get_json()["status"] == "unavailable"
        assert not ran  # handler must not execute on any substitute loop
    finally:
        loop.close()


def test_async_route_loopless_fallback_runtimeerror_returns_503():
    """No loop configured + handler raising the cross-loop RuntimeError
    (aiosqlite bound to a different loop) -> 503, not a propagated crash
    (the pre-fix behavior took the server down with exit code 2)."""
    app = Flask(__name__)  # GENESIS_EVENT_LOOP never set

    @_async_route
    async def cross_loop():
        raise RuntimeError("Lock is bound to a different event loop")

    with app.test_request_context("/"):
        resp, status = cross_loop()
    assert status == 503
    assert resp.get_json()["status"] == "unavailable"


def test_async_route_loopless_fallback_still_runs_handlers():
    """The loop-is-None fallback keeps executing plain handlers (unit-test
    contract) — only RuntimeError degrades to 503."""
    app = Flask(__name__)

    @_async_route
    async def plain():
        await asyncio.sleep(0)
        return "ok"

    with app.test_request_context("/"):
        assert plain() == "ok"
