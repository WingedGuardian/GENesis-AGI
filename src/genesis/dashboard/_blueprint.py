"""Shared blueprint infrastructure for Genesis dashboard routes."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import TimeoutError as FuturesTimeoutError
from functools import wraps
from pathlib import Path

from flask import Blueprint

logger = logging.getLogger("genesis.dashboard")

TEMPLATE_DIR = Path(__file__).parent / "templates"

blueprint = Blueprint(
    "genesis_dashboard",
    __name__,
    # Jinja search path for render_template — resolves relative to this package,
    # so /genesis renders identically under standalone AND Agent Zero hosting.
    template_folder="templates",
)


def _async_route(f=None, *, timeout: float | None = None):
    """Decorator to run async Flask route handlers.

    When the Genesis runtime loop is available (stored in
    ``app.config["GENESIS_EVENT_LOOP"]`` by the standalone adapter), the
    coroutine is dispatched to that loop via
    :func:`asyncio.run_coroutine_threadsafe`.  This ensures shared async
    objects (aiosqlite connections, asyncio locks, ``tracked_task`` calls)
    operate on the same event loop that created them.

    Flask 3.x stores request/app context on :mod:`contextvars` and
    ``asyncio.Task`` copies the calling thread's context automatically, so
    ``request``, ``current_app``, ``jsonify`` etc. remain accessible inside
    the coroutine even though it executes on the runtime thread.

    Falls back to a per-request ``new_event_loop()`` when no runtime loop
    is configured (e.g. during unit tests).

    ``timeout`` (seconds) optionally bounds how long the Flask worker thread
    waits for the coroutine. On expiry the route returns HTTP 503 instead of
    blocking indefinitely, so a slow handler can never make the endpoint look
    dead to the dashboard or the Guardian's health probe. ``None`` (default)
    preserves the original unbounded behavior — required for long-running
    routes (reply waiters, approval waits). Usable bare (``@_async_route``)
    or parametrized (``@_async_route(timeout=15)``).
    """

    def decorate(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            from flask import current_app, jsonify

            loop = current_app.config.get("GENESIS_EVENT_LOOP")
            if loop is not None and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(fn(*args, **kwargs), loop)
                try:
                    return future.result(timeout=timeout)
                except FuturesTimeoutError:
                    # Cancel the coroutine so it doesn't keep running on the loop
                    # after we've returned — otherwise repeated polling stacks
                    # concurrent snapshots (run_coroutine_threadsafe futures
                    # schedule task cancellation on the loop thread-safely).
                    future.cancel()
                    logger.warning(
                        "async route %s exceeded %.1fs timeout — returning 503",
                        getattr(fn, "__name__", "?"), timeout or 0.0,
                    )
                    return jsonify(
                        {"status": "unavailable", "error": "request timed out"}
                    ), 503

            # Fallback for tests or pre-runtime contexts
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(fn(*args, **kwargs))
            finally:
                loop.close()

        return wrapper

    return decorate(f) if f is not None else decorate
